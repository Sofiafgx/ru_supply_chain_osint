"""Run a project command from any working directory."""
from pathlib import Path
import os
import runpy
import sys

ROOT = Path(__file__).resolve().parent


def main():
    sys.path.insert(0, str(ROOT / 'src'))
    os.chdir(ROOT)
    if len(sys.argv) > 1 and sys.argv[1] == '--list':
        print('Commands in src/:')
        for path in sorted((ROOT / 'src').glob('*.py')):
            print('  ' + path.stem)
        print('  osint_pipeline (package CLI)')
        return
    if len(sys.argv) > 1 and not sys.argv[1].startswith('-'):
        command = sys.argv.pop(1)
        from sources_cli import PARSERS, main as sources_main
        if command in PARSERS:
            sys.argv.insert(1, command)
            raise SystemExit(sources_main())
        if command == 'osint_pipeline':
            runpy.run_module(command, run_name='__main__')
            return
        path = ROOT / 'src' / (command.removesuffix('.py') + '.py')
        if '/' in command:
            path = (ROOT / command).resolve()
            if not path.is_relative_to(ROOT / 'tools'):
                raise SystemExit('Only commands in src/ or tools/ are supported')
        if not path.is_file():
            raise SystemExit(f'Unknown command: {command}. Use --list.')
        sys.argv[0] = str(path)
        runpy.run_path(str(path), run_name='__main__')
    else:
        from sources_cli import main as sources_main
        raise SystemExit(sources_main())


if __name__ == '__main__':
    main()
