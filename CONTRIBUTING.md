# Contributing to MyEditor

Thank you for helping. You do not need to write code: some of the most useful help is a good bug report, a careful test of a new version, or a translation.

## Help without code

- **Report a problem.** [Open an issue](https://github.com/rinbal/my_editor/issues/new) and say what you did, what you expected and what happened instead, with your system and MyEditor's version (**MyEditor > About MyEditor** on a Mac, **Help > About MyEditor** elsewhere). **Help > Show Log Files** opens the log, which often shows the cause; look through it before you attach it, as it may name your files.
- **Test a new version.** Try what changed in the coming release ([What's new](docs/releases/README.md)) on your own system, in both languages and in light and dark, and report what feels wrong, not only what breaks.
- **Translate.** MyEditor speaks English and German. [Translating](docs/develop/translating.md) explains how to add a language or improve one, and holds the German style guide and glossary.
- **Improve the guide.** The [user guide](docs/README.md) should be right and easy to follow. If a page says something the app does not do, that is a bug too.
- **Found a security problem?** Please do not open an issue. [SECURITY.md](SECURITY.md) says how to report it privately.

## Run from source

The steps are in the README under [Run from source](README.md#run-from-source). To run the tests too, install the test dependencies:

```sh
pip install -r packaging/requirements-test.in
```

CI installs the same set with hashes, from `packaging/requirements-test.lock`. [How MyEditor is built](docs/develop/architecture.md) is a map of the code.

## Run the tests that matter

The whole suite runs on every pull request, on Windows, macOS and Linux, so you do not need to run it yourself. Run the tests for what you changed:

```sh
python -m pytest tests/test_find_replace.py           # one file
python -m pytest tests/test_find_replace.py -k case   # the tests whose name has "case"
```

Most test files are named after the module they test. On macOS and Linux, set `QT_QPA_PLATFORM=offscreen` so no windows open. Every run gets a home folder of its own, so the tests never touch your own settings.

If you changed a text people see, also run:

```sh
python scripts/i18n.py check
```

## Pull requests

- **Branch from `dev` and open the pull request against `dev`.** `main` holds released versions.
- **One topic per pull request**, explained by what a user gains, in plain words.
- **Look and feel.** MyEditor follows Apple's Human Interface Guidelines on every system: calm windows, one thing at a time, plain words in the interface. Show a change to the interface with screenshots, light and dark.
- **Texts.** Wrap every text a person sees for translation and add its German, as [Translating](docs/develop/translating.md) explains.
- **Docs.** Update the page a change affects, in the same pull request. The pull request template asks.
- **Shared files.** The photo group engine is shared with STANDUP and checked byte for byte ([decision 3](docs/decisions/0003-photo-groups-follow-standup.md)); change it in STANDUP first.
- **Licence.** By contributing, you agree that your work is published under the project's licence, AGPL-3.0-or-later.

## Conduct

Be kind and patient, assume good intent, and keep to the topic. Criticise work, never people. If someone treats you badly here, write to the maintainer, rinbal, privately rather than in public; it will be handled with care and in confidence.
