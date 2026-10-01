class SessionAbort(Exception):
    """Ends one account's session early. `status` goes into the session log."""

    def __init__(self, status: str, msg: str = ""):
        super().__init__(msg or status)
        self.status = status
