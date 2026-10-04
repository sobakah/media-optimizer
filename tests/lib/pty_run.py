#!/usr/bin/env python3
"""Run a command on a pseudo-terminal and answer its prompts.

Usage: pty_run.py "<answer1>|<answer2>|..." <command> [args...]

The scripts only ask questions when stdin is a terminal, so the interactive
paths cannot be tested with a pipe. Each answer is sent once the command has
been quiet for 0.4 s; an empty string sends just Enter. All output is printed
when the command ends (or after 20 s without output).
"""
import os
import pty
import select
import sys
import time

answers = sys.argv[1].split("|") if sys.argv[1] else []
cmd = sys.argv[2:]
pid, fd = pty.fork()
if pid == 0:
    os.execvp(cmd[0], cmd)

out = b""
ai = 0
last = time.time()
while True:
    ready, _, _ = select.select([fd], [], [], 0.3)
    if ready:
        try:
            data = os.read(fd, 4096)
        except OSError:
            break
        if not data:
            break
        out += data
        last = time.time()
    elif ai < len(answers) and time.time() - last > 0.4:
        os.write(fd, (answers[ai] + "\n").encode())
        ai += 1
        last = time.time()
    elif time.time() - last > 20:
        break
os.waitpid(pid, 0)
sys.stdout.write(out.decode(errors="replace"))
