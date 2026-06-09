import warnings

warnings.filterwarnings(
    "ignore",
    message='Field name "json" .* shadows an attribute in parent "BaseModel"',
    category=UserWarning,
)
