from .base import Adapter
from .chromium import Chromium
from .frozen import FrozenSession
from .onlyoffice import OnlyOffice
from .simple import Evince, Nautilus, VSCode
from .terminal import Terminal

ADAPTERS = [FrozenSession(), OnlyOffice(), Chromium(), Terminal(), Nautilus(), Evince(), VSCode()]


def adapter_for(w):
    key = " ".join(filter(None, [w.get("app_id"), w.get("wm_class"), w.get("gtk_app_id")]))
    return next((a for a in ADAPTERS if a.pattern.search(key)), Adapter())
