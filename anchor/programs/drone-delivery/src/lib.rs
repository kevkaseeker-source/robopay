use anchor_lang::prelude::*;
use anchor_lang::system_program;

declare_id!("3NmsWVX39uvzG3PBNPdSe4FTgudqSeLphJSbMDhV5F8Y");

#[program]
pub mod drone_delivery {
    use super::*;

    /// Buyer calls: locks GPS coordinates + SOL into the escrow PDA.
    pub fn create_delivery(
        ctx: Context<CreateDelivery>,
        amount: u64,
        target_lat: i64,
        target_lon: i64,
        deadline: i64,
    ) -> Result<()> {
        let escrow = &mut ctx.accounts.escrow;
        escrow.buyer          = ctx.accounts.buyer.key();
        escrow.seller         = ctx.accounts.seller.key();
        escrow.drone_operator = ctx.accounts.drone_operator.key();
        escrow.amount         = amount;
        escrow.target_lat     = target_lat;
        escrow.target_lon     = target_lon;
        escrow.deadline       = deadline;
        escrow.status         = DeliveryEscrow::STATUS_PENDING;
        escrow.bump           = ctx.bumps.escrow;

        let cpi_ctx = CpiContext::new(
            ctx.accounts.system_program.to_account_info(),
            system_program::Transfer {
                from: ctx.accounts.buyer.to_account_info(),
                to:   ctx.accounts.escrow.to_account_info(),
            },
        );
        system_program::transfer(cpi_ctx, amount)?;
        Ok(())
    }

    /// RPi4 calls: verify GPS -> payout SOL to the seller.
    pub fn confirm_delivery(
        ctx: Context<ConfirmDelivery>,
        actual_lat: i64,
        actual_lon: i64,
        timestamp: i64,
    ) -> Result<()> {
        let escrow = &ctx.accounts.escrow;

        require!(escrow.status == DeliveryEscrow::STATUS_PENDING, DroneError::NotPending);

        let clock = Clock::get()?;
        require!(clock.unix_timestamp <= escrow.deadline, DroneError::DeliveryExpired);

        // GPS tolerance ~22m lat, ~20m lon at 52°N
        let lat_diff = (actual_lat - escrow.target_lat).abs();
        let lon_diff = (actual_lon - escrow.target_lon).abs();
        require!(lat_diff <= 2000 && lon_diff <= 3000, DroneError::NotAtTarget);

        let amount = escrow.amount;

        **ctx.accounts.escrow.to_account_info().try_borrow_mut_lamports()? -= amount;
        **ctx.accounts.seller.try_borrow_mut_lamports()? += amount;

        let escrow = &mut ctx.accounts.escrow;
        escrow.status = DeliveryEscrow::STATUS_DELIVERED;

        emit!(DeliveryConfirmedEvent {
            buyer:      escrow.buyer,
            seller:     escrow.seller,
            amount,
            actual_lat,
            actual_lon,
            timestamp,
        });

        Ok(())
    }

    /// Buyer calls: refund after the deadline has expired.
    pub fn cancel_delivery(ctx: Context<CancelDelivery>) -> Result<()> {
        let escrow = &ctx.accounts.escrow;
        require!(escrow.status == DeliveryEscrow::STATUS_PENDING, DroneError::NotPending);
        let clock = Clock::get()?;
        require!(clock.unix_timestamp > escrow.deadline, DroneError::DeadlineNotReached);
        Ok(()) // close = buyer transfers all lamports back
    }

    /// Drone operator calls: close account after delivery (rent returned).
    ///
    /// Security-critical check: must run BEFORE Anchor's `close =
    /// drone_operator` constraint executes (which happens in the automatic
    /// post-instruction account-closing step, after this function body
    /// returns). Without it, an operator could call this on a still-PENDING
    /// escrow and receive the buyer's full deposit without ever calling
    /// confirm_delivery - exactly the fund-theft path flagged in Merdan's
    /// 2026-10-02 Colosseum pre-review. Returning an error here aborts the
    /// instruction before any lamports move.
    pub fn close_escrow(ctx: Context<CloseEscrow>) -> Result<()> {
        require!(
            ctx.accounts.escrow.status == DeliveryEscrow::STATUS_DELIVERED,
            DroneError::NotDelivered
        );
        Ok(())
    }
}

#[account]
pub struct DeliveryEscrow {
    pub buyer:          Pubkey,
    pub seller:         Pubkey,
    pub drone_operator: Pubkey,
    pub amount:         u64,
    pub target_lat:     i64,
    pub target_lon:     i64,
    pub deadline:       i64,
    pub status:         u8,
    pub bump:           u8,
}

impl DeliveryEscrow {
    pub const LEN: usize = 8 + 32 + 32 + 32 + 8 + 8 + 8 + 8 + 1 + 1;
    pub const STATUS_PENDING:   u8 = 0;
    pub const STATUS_DELIVERED: u8 = 1;
    pub const STATUS_CANCELLED: u8 = 2;
}

#[derive(Accounts)]
pub struct CreateDelivery<'info> {
    #[account(
        init,
        payer = buyer,
        space = DeliveryEscrow::LEN,
        seeds = [b"escrow", drone_operator.key().as_ref()],
        bump,
    )]
    pub escrow: Account<'info, DeliveryEscrow>,
    #[account(mut)]
    pub buyer: Signer<'info>,
    /// CHECK: seller pubkey is only stored
    pub seller: AccountInfo<'info>,
    /// CHECK: drone operator pubkey as PDA seed
    pub drone_operator: AccountInfo<'info>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct ConfirmDelivery<'info> {
    #[account(
        mut,
        seeds = [b"escrow", drone_operator.key().as_ref()],
        bump = escrow.bump,
        constraint = escrow.seller == seller.key() @ DroneError::WrongSeller,
    )]
    pub escrow: Account<'info, DeliveryEscrow>,
    pub drone_operator: Signer<'info>,
    /// CHECK: seller verified via constraint
    #[account(mut)]
    pub seller: AccountInfo<'info>,
}

#[derive(Accounts)]
pub struct CancelDelivery<'info> {
    #[account(
        mut,
        seeds = [b"escrow", escrow.drone_operator.as_ref()],
        bump = escrow.bump,
        close = buyer,
        constraint = buyer.key() == escrow.buyer @ DroneError::WrongBuyer,
    )]
    pub escrow: Account<'info, DeliveryEscrow>,
    #[account(mut)]
    pub buyer: Signer<'info>,
}

#[derive(Accounts)]
pub struct CloseEscrow<'info> {
    #[account(
        mut,
        seeds = [b"escrow", drone_operator.key().as_ref()],
        bump = escrow.bump,
        close = drone_operator,
    )]
    pub escrow: Account<'info, DeliveryEscrow>,
    #[account(mut)]
    pub drone_operator: Signer<'info>,
}

#[event]
pub struct DeliveryConfirmedEvent {
    pub buyer:      Pubkey,
    pub seller:     Pubkey,
    pub amount:     u64,
    pub actual_lat: i64,
    pub actual_lon: i64,
    pub timestamp:  i64,
}

#[error_code]
pub enum DroneError {
    #[msg("Escrow is not in Pending state")]
    NotPending,
    #[msg("Delivery deadline has expired")]
    DeliveryExpired,
    #[msg("Drone is not at target coordinates")]
    NotAtTarget,
    #[msg("Deadline not yet reached")]
    DeadlineNotReached,
    #[msg("Wrong seller account")]
    WrongSeller,
    #[msg("Wrong buyer account")]
    WrongBuyer,
    #[msg("Escrow must be in Delivered state to close")]
    NotDelivered,
}
