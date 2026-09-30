"""체험 화면들 (`/trial/...`)."""

from app.trial.pages.admin import build_admin
from app.trial.pages.edit import build_edit
from app.trial.pages.home import build_home
from app.trial.pages.session import build_session
from app.trial.pages.start import build_start


def create_trial_pages() -> None:
    build_home()
    build_start()
    build_session()
    build_edit()
    build_admin()
