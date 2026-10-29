"""launchd -> project bridge (2026-10-29, project moved to the external disk).

launchd cannot set a WorkingDirectory or StandardOut/ErrorPath on the
external volume (the job exits 78 before it starts), and a job whose own
executable is /bin/bash is refused the volume by TCC.  So every weather job
runs this file with the granted Python: it opens the job's log files, moves
into the project, runs the job's real command as a child (TCC attributes the
child's file access to this responsible process), forwards stop signals and
exits with the child's status.

usage: launchd_exec.py --cwd DIR --out PATH --err PATH -- CMD [ARGS...]
"""
import os, signal, subprocess, sys

args = sys.argv[1:]
split = args.index("--")
opts = dict(zip(args[:split:2], args[1:split:2]))
cmd = args[split + 1:]
out = open(opts["--out"], "ab", buffering=0)
err = open(opts["--err"], "ab", buffering=0) if opts["--err"] != opts["--out"] else out
os.chdir(opts["--cwd"])
# Same process group as this launcher, so launchd's SIGKILL at ExitTimeOut
# still reaches every process of the job; a stop signal is passed to the
# whole group because a bash wrapper waiting on its Python does not pass
# SIGTERM on (10-29: the resident outlived its bootout until killed, and in
# a group of its own it also outlived launchd's SIGKILL).
child = subprocess.Popen(cmd, stdout=out, stderr=err, stdin=subprocess.DEVNULL)


def forward(signum, _frame):
    signal.signal(signum, signal.SIG_IGN)
    try:
        os.killpg(os.getpgrp(), signum)
    except ProcessLookupError:
        pass


for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
    signal.signal(sig, forward)
code = child.wait()
sys.exit(code if code >= 0 else 128 - code)
