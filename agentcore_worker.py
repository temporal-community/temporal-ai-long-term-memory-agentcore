"""AgentCore direct-code entry point. No credentials are packaged with the code."""

from nicole.agentcore import create_app

if __name__ == "__main__":
    create_app().run()
