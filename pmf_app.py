"""Shared entry point for the portable Windows GUI, CLI, MCP and mount worker."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def main() -> None:
    arguments = sys.argv[1:]
    # Windowed PyInstaller launchers have no console streams.
    for name in ('stdout', 'stderr'):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, 'w', encoding='utf-8'))
    if arguments and arguments[0] == '--pmf-mount-server':
        from pmf_mount import main as mount_main
        sys.argv = [sys.argv[0], 'serve', *arguments[1:]]
        mount_main()
    elif arguments and arguments[0] == '--mcp':
        from pmfview_mcp import _queue, mcp
        _queue()
        mcp.run()
    elif arguments and arguments[0] == '--queue':
        from pmf_queue import main as queue_main
        sys.argv = [sys.argv[0], *arguments[1:]]
        queue_main()
    elif (not arguments or arguments[0] == '--gui'
          or Path(sys.executable).stem.lower() == 'pmfviewer'):
        from pmfview_gui import main as gui_main
        if arguments and arguments[0] == '--gui':
            sys.argv = [sys.argv[0], *arguments[1:]]
        gui_main()
    else:
        from pmfview import main as cli_main
        cli_main(arguments)


if __name__ == '__main__':
    from pmfview import PmfError
    try:
        main()
    except (PmfError, OSError) as exc:
        raise SystemExit(str(exc)) from exc
