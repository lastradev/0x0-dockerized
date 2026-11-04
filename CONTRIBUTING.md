# Contributing to 0x0-dockerized

Documentation:

- [Development](docs/development.md)
- [Architecture](docs/architecture.md)
- [API](docs/api.md)
- [Operations runbook](docs/runbook.md)

## Feature branches and documentation

Implementation and working feature notes stay on separate branches.

- Implementation branches (`feat/...`, `fix/...`) hold application code (`0x0/`), tests, templates, runtime scripts, and core docs (`README.md`, `docs/`).
- Feature documentation branches (`docs/...`) hold design specs, implementation plans, and exploration notes for that feature.
- Merge rule:
  - Do not merge `docs/...` branches into `master`. Leave them as historical branches.
  - Merge only the implementation branch (`feat/...` or `fix/...`) into `master`.
  - The merge commit must name the matching `docs/` branch (for example, `Working feature documentation and specs remain on docs/<feature-name>`).
  - Keep `README.md`, `docs/api.md`, `docs/architecture.md`, `docs/runbook.md`, and `docs/development.md` in sync with the code.

## Checklist

1. Add tests for new features or bug fixes in `0x0/tests/test_client.py`.
2. Run `~/.venvs/0x0/bin/python -m pytest tests/ -q` from `0x0/`.
3. Do not commit secrets (`FHOST_LINK_SECRET`, `.env`) or runtime data (`data/`).
4. Keep working specs on a `docs/` branch. Do not mix scratch files into the implementation branch.
5. Update `docs/` and `README.md` when you add or change endpoints, configuration variables, or behavior.
