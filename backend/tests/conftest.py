import os
import sys
import tempfile
import pytest

# Ensure backend directory is in path
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app import create_app
from app.database import db
from app.models.user import User
from app.models.workspace import Workspace
from app.auth.utils import generate_token


class TestConfig:
    TESTING = True
    DEBUG = False
    SECRET_KEY = "test-secret-key-codesage"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    CORS_ORIGINS = ["*"]
    DEFAULT_PROVIDER = "openai"
    OPENAI_API_KEY = "mock-openai-key-0123456789"
    ANTHROPIC_API_KEY = None
    GEMINI_API_KEY = None
    OPENROUTER_API_KEY = None
    KIMI_API_KEY = None
    OLLAMA_ENABLED = False
    MAX_EDIT_FILE_BYTES = 100_000
    INDEX_CACHE_DIR = tempfile.mkdtemp()
    WORKSPACE_ROOT = tempfile.mkdtemp()
    ALLOW_ANY_WORKSPACE_PATH = True
    ALLOW_GIT_CLONE = False
    RATE_LIMIT_REQUESTS_PER_MINUTE = 1000
    RATE_LIMIT_TOKENS_PER_DAY = 10_000_000
    MAX_CONCURRENT_REQUESTS_PER_USER = 5
    CONTEXT_MAX_TOKENS = 60_000
    HISTORY_MAX_TOKENS = 20_000
    COMMAND_TIMEOUT_SECONDS = 30
    COMMAND_MAX_OUTPUT_BYTES = 50_000
    LLM_FIRST_TOKEN_TIMEOUT = 10
    LLM_TOTAL_TIMEOUT = 30


@pytest.fixture
def app():
    app = create_app(config=TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def test_user(app):
    with app.app_context():
        user = User(email="dev@example.com", name="Test Developer")
        user.set_password("securepassword123")
        db.session.add(user)
        db.session.commit()
        u_id = user.id
        u_email = user.email

    class SimpleUser:
        def __init__(self, uid, email):
            self.id = uid
            self.email = email
    return SimpleUser(u_id, u_email)


@pytest.fixture
def auth_headers(app, test_user):
    with app.app_context():
        token = generate_token(test_user.id)
        return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def temp_repo():
    repo_dir = tempfile.mkdtemp()
    # Create some dummy files in the repo
    sample_file = os.path.join(repo_dir, "calculator.py")
    with open(sample_file, "w", encoding="utf-8") as f:
        f.write("def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n")

    readme_file = os.path.join(repo_dir, "README.md")
    with open(readme_file, "w", encoding="utf-8") as f:
        f.write("# Calculator Project\nSimple math library.\n")

    yield repo_dir
