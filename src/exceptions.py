class StripeAutomationError(Exception):
    """Base exception for the stripe automation system."""


class ConfigError(StripeAutomationError):
    """Raised when configuration is invalid or missing."""


class StripeAPIError(StripeAutomationError):
    """Raised when Stripe API calls fail."""


class StaleClassificationError(StripeAutomationError):
    """Raised when stored classifications contradict the current rules (reclassify first)."""


class ReportAlreadyFrozenError(StripeAutomationError):
    """Raised when freezing a quarter that already has a declared report."""
