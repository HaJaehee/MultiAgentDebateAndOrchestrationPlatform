"""쉬운 화면들 (`/trial/easy/...`)."""

from app.easy.pages.build import build_builder
from app.easy.pages.home import build_home
from app.easy.pages.run import build_run
from app.easy.pages.session import build_session


def create_easy_pages() -> None:
    build_home()
    build_builder()
    build_run()
    build_session()
