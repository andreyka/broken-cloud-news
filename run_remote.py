import pexpect
import sys

def run_remote(command):
    child = pexpect.spawn(f'ssh -o StrictHostKeyChecking=no avkov@192.168.0.37 "{command}"', encoding='utf-8')
    index = child.expect(['password:', pexpect.EOF, pexpect.TIMEOUT], timeout=300)
    if index == 0:
        child.sendline('2306')
        child.expect(pexpect.EOF, timeout=300)
        print(child.before)
    elif index == 1:
        print(child.before)
    else:
        print("Timeout")

if __name__ == "__main__":
    run_remote(sys.argv[1])
