class StripeAutomationError(Exception):
    """Base exception for the stripe automation system."""


class ClassificationError(StripeAutomationError):
    """Raised when a transaction cannot be classified."""


class ValidationError(StripeAutomationError):
    """Raised when validation of classified data fails."""


class CSVParseError(StripeAutomationError):
    """Raised when CSV parsing fails."""


class ConfigError(StripeAutomationError):
    """Raised when configuration is invalid or missing."""


class StripeAPIError(StripeAutomationError):
    """Raised when Stripe API calls fail."""
