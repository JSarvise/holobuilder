# mitmproxy addon: relax HTTP/2 header validation. The game sends a header value
# with trailing whitespace (CPU model), which h2's _reject_illegal_characters
# rejects with "Received header value surrounded by whitespace", aborting the
# stream. Replace that validator with a pass-through so we can capture the gRPC
# traffic. (Config-level toggles didn't stick; patch the function directly.)
import h2.utilities

def _passthrough(headers, *args, **kwargs):
    for h in headers:
        yield h

h2.utilities._reject_illegal_characters = _passthrough

# belt-and-suspenders: also relax config defaults
import h2.config
_orig_init = h2.config.H2Configuration.__init__
def _patched_init(self, *args, **kwargs):
    kwargs["validate_inbound_headers"] = False
    kwargs["normalize_inbound_headers"] = True
    _orig_init(self, *args, **kwargs)
h2.config.H2Configuration.__init__ = _patched_init

print("[addon] h2 _reject_illegal_characters neutralized", flush=True)
