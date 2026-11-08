# Fonecta Connect prediction engine

The submitted C13 model achieved a **70.92 utility score** (three-seed grouped 2026 cross-validation mean). The frozen inference bundle includes all seven model partitions, public-data features and reference predictions. The browser and voice demos already have their exports and do not need inference at startup.

## Run

From the repository root, using Python 3.12:

```sh
python -m pip install -r engine/requirements.txt
python -X utf8 engine/run.py
```

Use `--output <directory>` for a separate output directory. `--data` and `--targets` can select the supplied package in another location. On macOS/Linux, `engine/run.sh` is also available. Windows users may substitute `py -3.12` for `python`.

The command verifies public-data and model fingerprints, runs all seven partitions and compares the result with the frozen reference. It produces `predictions_2026.csv`, `demo_suggestions.json`, `voice_suggestions.json`, `review_suggestions.json` and `run_report.json` under `output/`. It sends no messages and changes no listings.

## Configuration and limits

`config.json` records the model, snapshot, blend and policy settings. `models/c13_v1/` contains the learned artifacts. This is a November 2026 batch model for the supplied roster; changed data or new businesses require a new validated export. The runtime does not read labels, simulator personas, secrets or research code. Historical peer aggregates and adaptively explored rules limit how much the benchmark says about future traffic.

`demo_suggestions.json` covers every demo date, including NO_ACTION. `voice_suggestions.json` contains contact-eligible model suggestions with numeric confidence and review flags; rule proposals without measured confidence remain in `review_suggestions.json`. Opt-outs are preserved. A proposal never authorizes a write by itself.

## Verification

```sh
python -m unittest discover -s engine/tests
python -X utf8 engine/tests/run_isolated.py --output .runtime/engine-check --save-verification
python -X utf8 engine/tests/verify_outputs.py --output .runtime/engine-check
python -X utf8 engine/validate_submission.py --predictions predictions_2026.csv --targets "resources/evaluation/targets_2026.csv"
python -X utf8 engine/tests/verify_voice_contract.py
```

The isolated runner blocks labels, secrets, simulator state and research imports. Its report includes reference parity and partition coverage. Model files retain their exact bytes across platforms; LightGBM reads them as UTF-8 text, including when the checkout path contains non-ASCII characters.

Python and dependency licences are listed in the [root README](../README.md#third-party-code-data-and-licences). The original feature implementations and frozen export provenance are recorded in `vendor_manifest.json` and `models/c13_v1/model_card.json`.
