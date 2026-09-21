# main.py
import os

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic

# Load CLAUDE_CODE_OAUTH_TOKEN from a .env file
load_dotenv()

# Initialize the LLM. langchain_anthropic only auto-reads ANTHROPIC_API_KEY,
# so an OAuth-style auth token has to be passed via default_headers instead.
llm = ChatAnthropic(
    model="claude-haiku-4-5",
    default_headers={"Authorization": f"Bearer {os.environ['CLAUDE_CODE_OAUTH_TOKEN']}"},
)

# Invoke it
response = llm.invoke("What is LangChain?")

print(response.content)