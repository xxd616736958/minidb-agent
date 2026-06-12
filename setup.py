"""Minimal package setup for editable install."""
from setuptools import setup, find_packages

setup(
    name="zuixiaoagent",
    version="0.1.0",
    description="Terminal-operating programming intelligent agent based on LangChain ecosystem",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[
        "langgraph>=0.6.0",
        "langchain>=0.3.0",
        "langchain-core>=0.3.0",
        "langchain-openai>=0.2.0",
        "pydantic>=2.10.0",
        "psycopg[binary]>=3.2.0",
        "pglast>=6.0",
        "langsmith>=0.2.0",
    ],
)
