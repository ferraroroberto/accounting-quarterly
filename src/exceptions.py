class StripeAutomationError(Exception):
    """Base exception for the stripe automation system."""


class ConfigError(StripeAutomationError):
    """Raised when configuration is invalid or missing."""


class StripeAPIError(StripeAutomationError):
    """Raised when Stripe API calls fail."""
