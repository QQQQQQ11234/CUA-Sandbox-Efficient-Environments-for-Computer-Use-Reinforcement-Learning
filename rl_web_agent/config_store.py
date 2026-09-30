"""
Global configuration store for singleton access across the application.
"""

from omegaconf import DictConfig


class ConfigStore:
    """Singleton class for storing and accessing configuration globally"""

    cfg: DictConfig = None

    @classmethod
    def set(cls, config: DictConfig) -> None:
        """Set the global configuration"""
        cls.cfg = config

    @classmethod
    def get(cls) -> DictConfig:
        """Get the global configuration"""
        if cls.cfg is None:
            raise RuntimeError("Configuration not set. Call ConfigStore.set() from your main function first.")
        return cls.cfg

    @classmethod
    def reset(cls) -> None:
        """Reset the configuration (useful for testing)"""
        cls.cfg = None
