"""Built-in coding adapters. SDK imports are lazy so unused providers are optional."""

from factory.application.ports import AgentCapabilities, CodingRequest
from factory.domain.models import ModelError


class APICodingAgent:
    capabilities = AgentCapabilities(context="selected-files")

    def __init__(self, model_factory):
        self.model_factory = model_factory

    def generate(self, request: CodingRequest):
        return self.model_factory().generate_patch(
            title=request.title,
            body=request.body,
            comments=request.comments,
            files=request.files,
            existing_paths=request.existing_paths,
            review_feedback=request.review_feedback,
        )


class CLICodingAgent:
    capabilities = AgentCapabilities(context="repository-archive")

    def __init__(
        self,
        agent: str,
        *,
        max_iterations=10,
        runner=None,
        settings=None,
        credentials=None,
        agent_home=None,
    ):
        self.credentials = credentials
        self.agent_home = agent_home
        self.agent = agent
        self.max_iterations = max_iterations
        self.runner = runner
        self.settings = settings

    def generate(self, request: CodingRequest):
        from cli_agent import generate_patch

        if request.archive is None:
            raise ModelError("CLI execution requires a repository archive")
        return generate_patch(
            agent=self.agent,
            **(
                {"credentials": self.credentials, "agent_home": self.agent_home}
                if self.credentials is not None
                else {}
            ),
            settings=self.settings,
            runner=self.runner,
            max_iterations=self.max_iterations,
            title=request.title,
            body=request.body,
            comments=request.comments,
            existing_paths=request.existing_paths,
            archive=request.archive,
            review_feedback=request.review_feedback,
        )


def openai_model(settings, *, credentials=None, agent_home=None):
    from model_client import ModelClient

    return ModelClient(
        max_patch_iterations=settings.max_patch_iterations,
        settings=settings,
        **(
            {"api_key": credentials.get("openai", "")}
            if credentials is not None
            else {}
        ),
    )


def api_coding(settings, **kwargs):
    return APICodingAgent(lambda: openai_model(settings, **kwargs))


def codex_coding(settings, **kwargs):
    return CLICodingAgent(
        "codex",
        max_iterations=settings.max_patch_iterations,
        settings=settings,
        **kwargs,
    )


def claude_coding(settings, **kwargs):
    return CLICodingAgent(
        "claude",
        max_iterations=settings.max_patch_iterations,
        settings=settings,
        **kwargs,
    )
