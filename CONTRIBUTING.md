# Contributing to ClickScope

Thank you for helping. ClickScope has one maintainer, [@sgr-xd](https://github.com/sgr-xd), and
welcomes issues and pull requests.

## Before you start

- **Bugs and ideas:** open an issue first for anything bigger than a small fix, so we agree on
  the approach before you spend time on it.
- **Security problems:** never in a public issue. See [SECURITY.md](SECURITY.md).
- **Conduct:** this project follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Making a change

1. Fork the repository and branch from `main`.
2. Set up: Python 3.10 or later:

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
```
3. Run the checks before you push:

   ```sh
   ruff check .
   pytest -m 'not integration'    # integration tests need a real ClickHouse; see tests/test_integration.py
   ```

4. Keep one change per pull request. New behaviour needs tests.
5. Add a line to `CHANGELOG.md` under **Unreleased**.
6. Open a pull request against `main`. `main` takes changes only through pull requests whose
   checks pass: no direct pushes, no force pushes, linear history.

## Rules this project keeps

- ClickScope is read-only. A change must never let a query write, change settings, or read past the policy's limits; the policy has adversarial tests, and a new path through it needs one too.
- Never commit a password, a credential file or an audit log. Tests that need a server read it from the environment.
- Never use real customer, tenant or company data in examples, tests or fixtures. Use made-up names such as `acme`.
- Few dependencies; justify any new one in the pull request.

## License

By contributing, you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE), the license of this project.
