# Generic Feature Patterns

- Rolling and lag target features must shift history before aggregation.
- Target encoding must use expanding or prior-window history only.
- Feature names should include the requested `feature_name` or a stable token derived from it.
- Features must reach the model feature list, not only an intermediate DataFrame.

