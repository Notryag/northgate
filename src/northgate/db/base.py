from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


from northgate.db import models as models  # noqa: E402, F401
