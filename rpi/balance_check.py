import serial
import time

def send_at_command(command, result_filter, device='/dev/ttyUSB2', baudrate=115200, timeout=30):
    try:
        ser = serial.Serial(port=device, baudrate=baudrate, timeout=timeout)
        print(f"Connected to {device}")

        ser.flushInput()
        ser.flushOutput()

        ser.write(command)
        while True:
            line = ser.readline()
            if (result_filter in str(line)) == True:
                print(line)
                return

    except serial.SerialException as e:
        print(f"Serial communication error: {e}")
    finally:
        ser.close()

send_at_command(command=b'AT+CSQ\r', result_filter="+CSQ:")
send_at_command(command=f'AT+CUSD=1,"*100#",15\r'.encode(), result_filter="+CUSD:")
send_at_command(command=f'AT+CUSD=1,"*102#",15\r'.encode(), result_filter="+CUSD:")
send_at_command(command=f'AT+CUSD=1,"*135#",15\r'.encode(), result_filter="+CUSD:")
