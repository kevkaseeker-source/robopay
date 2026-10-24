use anchor_lang::prelude::*;

declare_id!("Fg6PaFpoGXkYsidMpWxTWqkZL6D5F5F5y7gVZV5V1Gk");

#[program]
pub mod machine_split {
    use super::*;

    pub fn init_split(ctx: Context<InitSplit>, machine_id: [u8; 32], owners: Vec<Pubkey>, bps: Vec<u16>) -> Result<()> {
        validate_shares(&owners, &bps)?;
        let split = &mut ctx.accounts.split;
        split.machine_id = machine_id;
        split.authority = ctx.accounts.authority.key();
        split.owners = owners;
        split.bps = bps;
        split.total_received = 0;
        split.bump = ctx.bumps.split;
        Ok(())
    }

    pub fn pay_delivery(ctx: Context<PayDelivery>, amount: u64) -> Result<()> {
        require!(amount > 0, SplitError::ZeroAmount);
        let ix = anchor_lang::solana_program::system_instruction::transfer(
            &ctx.accounts.payer.key(), &ctx.accounts.split.key(), amount,
        );
        anchor_lang::solana_program::program::invoke(
            &ix, &[ctx.accounts.payer.to_account_info(), ctx.accounts.split.to_account_info(), ctx.accounts.system_program.to_account_info()],
        )?;
        ctx.accounts.split.total_received = ctx.accounts.split.total_received.checked_add(amount).ok_or(SplitError::MathOverflow)?;
        Ok(())
    }

    pub fn distribute(ctx: Context<Distribute>) -> Result<()> {
        let split = &ctx.accounts.split;
        require!(ctx.remaining_accounts.len() == split.owners.len(), SplitError::OwnerAccountsMismatch);
        let rent_floor = Rent::get()?.minimum_balance(split.to_account_info().data_len());
        let available = split.to_account_info().lamports().checked_sub(rent_floor).ok_or(SplitError::InsufficientVault)?;
        require!(available > 0, SplitError::EmptyVault);
        let mut assigned = 0u64;
        let owner_count = split.owners.len();
        for (i, recipient) in ctx.remaining_accounts.iter().enumerate() {
            require!(recipient.key() == split.owners[i], SplitError::OwnerAccountsMismatch);
            require!(recipient.is_writable, SplitError::RecipientNotWritable);
            let amount = if i + 1 == owner_count {
                available.checked_sub(assigned).ok_or(SplitError::MathOverflow)?
            } else {
                let part = (available as u128).checked_mul(split.bps[i] as u128).ok_or(SplitError::MathOverflow)? / 10_000;
                let part = u64::try_from(part).map_err(|_| error!(SplitError::MathOverflow))?;
                assigned = assigned.checked_add(part).ok_or(SplitError::MathOverflow)?;
                part
            };
            **split.to_account_info().try_borrow_mut_lamports()? = split.to_account_info().lamports().checked_sub(amount).ok_or(SplitError::InsufficientVault)?;
            **recipient.try_borrow_mut_lamports()? = recipient.lamports().checked_add(amount).ok_or(SplitError::MathOverflow)?;
        }
        Ok(())
    }

    pub fn update_shares(ctx: Context<UpdateShares>, owners: Vec<Pubkey>, bps: Vec<u16>) -> Result<()> {
        validate_shares(&owners, &bps)?;
        let floor = Rent::get()?.minimum_balance(ctx.accounts.split.to_account_info().data_len());
        require!(ctx.accounts.split.to_account_info().lamports() <= floor, SplitError::VaultNotEmpty);
        ctx.accounts.split.owners = owners;
        ctx.accounts.split.bps = bps;
        Ok(())
    }
}

fn validate_shares(owners: &[Pubkey], bps: &[u16]) -> Result<()> {
    require!(!owners.is_empty() && owners.len() <= 8, SplitError::InvalidOwnerCount);
    require!(owners.len() == bps.len(), SplitError::ShareLengthMismatch);
    require!(owners.iter().all(|key| *key != Pubkey::default()), SplitError::InvalidOwner);
    require!(owners.iter().enumerate().all(|(i, key)| !owners[..i].contains(key)), SplitError::DuplicateOwner);
    require!(bps.iter().all(|share| *share > 0), SplitError::ZeroShare);
    let sum: u32 = bps.iter().map(|share| *share as u32).sum();
    require!(sum == 10_000, SplitError::InvalidShareTotal);
    Ok(())
}

#[derive(Accounts)]
#[instruction(machine_id: [u8; 32])]
pub struct InitSplit<'info> {
    #[account(init, payer = authority, space = SplitAccount::SPACE, seeds = [b"split", machine_id.as_ref()], bump)]
    pub split: Account<'info, SplitAccount>,
    #[account(mut)] pub authority: Signer<'info>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct PayDelivery<'info> {
    #[account(mut)] pub split: Account<'info, SplitAccount>,
    #[account(mut)] pub payer: Signer<'info>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct Distribute<'info> {
    #[account(mut)] pub split: Account<'info, SplitAccount>,
}

#[derive(Accounts)]
pub struct UpdateShares<'info> {
    #[account(mut, has_one = authority, seeds = [b"split", split.machine_id.as_ref()], bump = split.bump)]
    pub split: Account<'info, SplitAccount>,
    pub authority: Signer<'info>,
}

#[account]
pub struct SplitAccount {
    pub machine_id: [u8; 32],
    pub authority: Pubkey,
    pub owners: Vec<Pubkey>,
    pub bps: Vec<u16>,
    pub total_received: u64,
    pub bump: u8,
}
impl SplitAccount { pub const SPACE: usize = 8 + 32 + 32 + 4 + 8 * 32 + 4 + 8 * 2 + 8 + 1; }

#[error_code]
pub enum SplitError {
    #[msg("Owner count must be between 1 and 8")] InvalidOwnerCount,
    #[msg("Owners and shares must have matching lengths")] ShareLengthMismatch,
    #[msg("Shares must sum to exactly 10000 basis points")] InvalidShareTotal,
    #[msg("Owner key cannot be the default key")] InvalidOwner,
    #[msg("Owner keys must be unique")] DuplicateOwner,
    #[msg("Every owner must have a non-zero share")] ZeroShare,
    #[msg("Remaining accounts must match the configured owners in order")] OwnerAccountsMismatch,
    #[msg("Every recipient account must be writable")] RecipientNotWritable,
    #[msg("Vault has no distributable lamports")] EmptyVault,
    #[msg("Vault balance is below its rent floor")] InsufficientVault,
    #[msg("Update shares only after the vault is empty")] VaultNotEmpty,
    #[msg("Payment amount must be greater than zero")] ZeroAmount,
    #[msg("Arithmetic overflow")] MathOverflow,
}
