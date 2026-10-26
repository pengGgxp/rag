"""Keep one-time callback credentials out of application HTTP access logs."""
import logging


class AuthenticationAccessFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            address, method, path, protocol, status = record.args
            if isinstance(path, str) and "/auth/" in path and "/callback" in path:
                path = path.split("/callback", 1)[0] + "/callback/[redacted]"
                record.args = (address, method, path, protocol, status)
        return True


logging.getLogger("uvicorn.access").addFilter(AuthenticationAccessFilter())


class ProviderCredentialFilter(logging.Filter):
    def filter(self, record):
        return "u.arsn.cn/connect.php" not in record.getMessage()


logging.getLogger("httpx").addFilter(ProviderCredentialFilter())
