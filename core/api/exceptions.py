"""Import-light public endpoint exception, also exposed by the plugin SDK."""


class ApiError(Exception):
    def __init__(self, code, message, status=400, *, retry_after=None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.retry_after = retry_after

    def as_dict(self):
        data = {"code": self.code, "message": self.message}
        if self.retry_after is not None:
            data["retry_after"] = self.retry_after
        return data
