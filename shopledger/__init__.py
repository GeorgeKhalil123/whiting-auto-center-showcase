"""shopledger — offline invoicing backend for a single shop desktop.

SQLite on disk, a thin repository/service layer, and a transactional `Api`
facade. In the production app the facade is exposed to a pywebview window as
its JS bridge; here it runs headless so the backend can be exercised from the
command line and from tests.
"""

__version__ = "1.8.0"
APP_NAME = "ShopLedger"
