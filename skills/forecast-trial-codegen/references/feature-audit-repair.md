# Feature Audit Repair

- Add executable evidence: a real `df[feature_col] = ...`, helper call, or CLI value consumed outside parsing.
- Put the requested feature token in the column/helper name, for example `zero_history_flag`.
- Ensure the feature is created before `feature_cols` is returned or before the model selects columns.
- If evidence is in a dependency file, ensure `train.py` imports and calls that dependency path.
- Metadata, comments, unused helper definitions, and unconsumed argparse flags do not count as applied features.
