# Signature Repair

- Copy the original function signature exactly when using `replace_function`.
- Do not remove accepted keyword parameters, default values, `*args`, `**kwargs`, or keyword-only parameters.
- Do not add new required parameters unless all existing callers already pass them.
- Preserve return arity. If the original may return `(df, feature_cols, cat_cols, maps)`, the replacement must still support that path.
- Prefer inserting lines into the existing function over replacing it when the failure is only a missing feature column.
