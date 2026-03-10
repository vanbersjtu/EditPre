"""CLI entrypoint for compiler package.

Stage-1 refactor: delegate to legacy monolithic implementation while
keeping command behavior unchanged.
"""

from .legacy_svg_to_pptx_pro import main as legacy_main


def main() -> None:
    legacy_main()


if __name__ == "__main__":
    main()
