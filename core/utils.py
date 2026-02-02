import functools
import pandas as pd
from pandasai_litellm import LiteLLM
from django.conf import settings


@functools.lru_cache(maxsize=1)
def get_llm_client():
    """
    Singleton LLM client for PandasAI using LiteLLM.

    Returns:
        LiteLLM: Configured LLM instance compatible with PandasAI
    """
    return LiteLLM(
        model="claude-sonnet-4-5-20250929",
        api_key=settings.ANTHROPIC_API_KEY,
    )


@functools.lru_cache(maxsize=32)
def get_dataframe_cached(dataset_id: str, file_path: str) -> pd.DataFrame:
    """
    Cache DataFrames in memory to avoid repeated file reads.

    Args:
        dataset_id: UUID of the dataset (for cache key)
        file_path: Path to the data file

    Returns:
        pd.DataFrame: Parsed DataFrame
    """
    if file_path.endswith('.csv'):
        return pd.read_csv(file_path)
    else:
        return pd.read_excel(file_path)
