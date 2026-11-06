# Real-Debrid Egress Troubleshooting

Use this runbook when the backend reports `Circuit breaker OPEN for api.real-debrid.com`.

## Safety

- Run network checks inside the backend container, not only on the host.
- Do not put Real-Debrid API keys, proxy usernames/passwords, or full credential-bearing proxy URLs in shell arguments, command history, screenshots, or logs.
- Use the existing Settings connection test for authenticated validation; it uses the configured downloader proxy and returns sanitized transport failures.
- Do not restart the backend or clear application state until diagnostic output is captured. The breaker is in-memory and will reopen if the underlying transport fault remains.

## Checks from the backend container

1. Record the container name/ID and timestamp. Confirm whether the expected route is direct egress or `downloaders.proxy_url`.
2. Check DNS resolution for `api.real-debrid.com` inside the container, for example with `docker exec <backend-container> python -c "import socket; print(socket.getaddrinfo('api.real-debrid.com', 443))"`. Record the resolved addresses or resolver error.
3. Test HTTPS/TLS reachability without credentials, for example with `docker exec <backend-container> curl --silent --show-error --output /dev/null --write-out "HTTP %{http_code}\n" --connect-timeout 5 --max-time 10 https://api.real-debrid.com/rest/1.0/user`. A response with HTTP `401` is evidence that DNS, TCP, TLS, and HTTP reached Real-Debrid; it does not validate the API key. Record timeout, TLS, and name-resolution failures distinctly.
4. If the deployment expects the shared downloader proxy, perform the equivalent reachability check through that proxy without printing the proxy URL. Compare direct and proxy results only when direct egress is permitted by deployment policy.
5. If unauthenticated reachability succeeds, run the existing Settings connection test to validate credentials and the configured route. Do not manually place the bearer token in a command line. If authenticated validation through a proxy is needed, use that Settings test rather than shelling out with a credential-bearing proxy URL.

## Interpret and recover

- **DNS lookup fails inside the container:** investigate container resolver configuration and DNS reachability from its network.
- **DNS succeeds but HTTPS/TLS fails:** investigate outbound TCP 443, firewall/egress policy, TLS interception, or proxy reachability.
- **Direct fails, proxy works:** confirm the configured proxy is the intended production route; correct proxy configuration through the supported settings path if necessary.
- **Transport works but Settings reports 401/403:** check the configured Real-Debrid credential/account status using the approved secret-management process.
- **Transport and authenticated connection both succeed:** the breaker should admit a half-open probe after its recovery interval; inspect sanitized application logs for subsequent provider responses.

The breaker trips after repeated failures and permits one half-open request after 30 seconds. A successful probe closes it; an ongoing transport failure reopens it. The resolver skips redundant fallback work for a provider already reporting OPEN and continues to other providers. Host-side DNS success does not establish container-side DNS or egress. Keep the incident cause unconfirmed until container evidence is collected.
