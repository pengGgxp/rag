"""Payment source IP resolution; the ASGI client must remain the transport peer."""
from functools import lru_cache
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network

from starlette.requests import Request


@lru_cache(maxsize=32)
def trusted_proxy_networks(config: str) -> tuple[IPv4Network | IPv6Network, ...]:
    if not config.strip():
        return ()
    try:
        networks = tuple(ip_network(item.strip(), strict=False) for item in config.split(","))
    except ValueError:
        raise ValueError("payment trusted proxies must be comma-separated IP addresses or CIDRs") from None
    if any(network.prefixlen == 0 for network in networks):
        raise ValueError("payment trusted proxies must not trust all addresses")
    return networks


def _address(value: str) -> IPv4Address | IPv6Address:
    # Forwarded values must be bare IP literals, not hostnames, ports or zone IDs.
    if "%" in value:
        raise ValueError("scoped addresses are not supported")
    address = ip_address(value)
    return address.ipv4_mapped if address.version == 6 and address.ipv4_mapped else address


def payment_client_ip(request: Request, trusted_proxies: str) -> str:
    """Walk XFF right to left, stopping at the first untrusted hop.

    Missing/malformed forwarding information falls back to the connection peer.
    X-Real-IP and Forwarded are deliberately not alternative trust paths.
    """
    try:
        peer = _address(request.client.host) if request.client else None
    except ValueError:
        peer = None
    if peer is None:
        return ""
    networks = trusted_proxy_networks(trusted_proxies)
    current = peer
    forwarded = ",".join(request.headers.getlist("x-forwarded-for"))
    if not forwarded:
        return str(peer)
    for value in reversed(forwarded.split(",")):
        if not any(current in network for network in networks):
            break
        try:
            current = _address(value.strip())
        except ValueError:
            return str(peer)
    return str(current)
