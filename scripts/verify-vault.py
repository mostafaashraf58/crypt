"""Exercise only a disposable credential; never read configured account secrets."""
import keyring
from uuid import uuid4

service="Axiom-QA-"+uuid4().hex
try:
    keyring.set_password(service,"temporary-verification","non-sensitive-test-value")
    assert keyring.get_password(service,"temporary-verification")=="non-sensitive-test-value"
    print("PASS Windows vault stores and reloads a temporary credential")
finally:
    if keyring.get_password(service,"temporary-verification") is not None:
        keyring.delete_password(service,"temporary-verification")
assert keyring.get_password(service,"temporary-verification") is None
print("PASS temporary credential removed; existing credentials untouched")
