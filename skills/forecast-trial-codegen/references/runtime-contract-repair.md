# Runtime Contract Repair

- Read the callee signature from `source_locator.signature_constraints` or `contract_errors`.
- Fix the exact failing call: pass missing required parameters and remove unknown keywords or extra positional args.
- Use source variables that already exist in scope, such as `date_col`, `label_col`, `id_cols`, `group_cols`, or `args`.
- Do not invent evaluation artifact columns such as `entity_id` if the copied source has generic id/date/label parameters.
- Keep the repair local to the call site or helper definition that failed contract validation.
