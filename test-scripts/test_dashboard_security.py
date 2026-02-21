import sys
import os
from pathlib import Path

# Add root to sys.path
root = Path(__file__).parent.parent
sys.path.insert(0, str(root))

# Mock some environment variables
os.environ["MASTER_DASHBOARD_PUBLIC_URL"] = "https://admin.earlysalty.de"

from service.dashboard import DashboardServer

def test_normalization():
    norm = DashboardServer._normalize_auth_next_path
    
    print("Testing normalization...")
    
    # Positive cases
    assert norm("/admin") == "/admin"
    assert norm("/admin/logs") == "/admin/logs"
    assert norm("/api/v1/user") == "/api/v1/user"
    assert norm("/turnier") == "/turnier"
    
    # Query parameters should be preserved if path is safe
    assert norm("/admin?foo=bar") == "/admin?foo=bar"
    
    # Basic bypasses
    assert norm("http://evil.com") == "/admin"
    assert norm("//evil.com") == "/admin"
    assert norm(r"\\evil.com") == "/admin"
    assert norm(r"/\evil.com") == "/admin"
    
    # Path traversal bypasses
    assert norm("/admin/../evil.com") == "/admin"
    assert norm("/turnier/..//evil.com") == "/admin"
    assert norm("/admin/./logs") == "/admin/logs"
    
    # Newline injection
    assert norm("/admin\r\nLocation: http://evil.com") == "/admin"
    
    # Backslash as slash bypass
    assert norm(r"/admin\..\..\evil.com") == "/admin"

    print("Normalization tests passed!")

def test_redirect():
    safe = DashboardServer._safe_internal_redirect
    
    print("Testing safe internal redirect...")
    
    assert safe("/admin") == "/admin"
    assert safe("//evil.com") == "/admin"
    assert safe("/..//evil.com") == "/admin"
    assert safe("/admin/../evil.com") == "/admin"
    assert safe(r"/admin\..\evil.com") == "/admin"
    
    print("Redirect tests passed!")

if __name__ == "__main__":
    try:
        test_normalization()
        test_redirect()
    except AssertionError as e:
        print("Tests failed!")
        raise
    except Exception as e:
        print(f"Error during tests: {e}")
        raise
