"""
Test für die Bild-Extraktion Fix
"""
import requests
import re

def test_cdn_selection(img_url_with_placeholder):
    """Testet die CDN-Auswahl für Steam Bilder"""
    print(f"Original Platzhalter: {img_url_with_placeholder}")
    
    if img_url_with_placeholder.startswith('{STEAM_CLAN_IMAGE}'):
        # Teste sowohl fastly als auch akamai CDN
        fastly_url = img_url_with_placeholder.replace('{STEAM_CLAN_IMAGE}', 'https://clan.fastly.steamstatic.com/images/')
        akamai_url = img_url_with_placeholder.replace('{STEAM_CLAN_IMAGE}', 'https://clan.akamai.steamstatic.com/images/')
        
        print(f"Teste fastly: {fastly_url}")
        print(f"Teste akamai: {akamai_url}")
        
        # Teste welche URL verfügbar ist
        try:
            fastly_response = requests.head(fastly_url, timeout=5)
            print(f"Fastly Status: {fastly_response.status_code}")
            if fastly_response.status_code == 200:
                final_url = fastly_url
                print(f"SUCCESS: Verwende fastly CDN: {final_url}")
            else:
                final_url = akamai_url
                print(f"WARNING: Fallback zu akamai CDN: {final_url}")
        except Exception as e:
            final_url = akamai_url  # Fallback zu akamai
            print(f"ERROR: CDN-Test fehlgeschlagen ({e}), verwende akamai: {final_url}")
        
        return final_url
    
    return img_url_with_placeholder

# Test mit dem Billy Bild
billy_placeholder = "{STEAM_CLAN_IMAGE}/45164767/6155ec51cb83504f4649748ee9be6cce27920329.png"
result = test_cdn_selection(billy_placeholder)
print(f"\nFinale URL: {result}")

# Test mit dem falschen Bild  
wrong_placeholder = "{STEAM_CLAN_IMAGE}/45164767/f67ecaff28204a3d8d9ab86f495a3e4465df3135.png"
result2 = test_cdn_selection(wrong_placeholder)
print(f"\nFinale URL (falsches Bild): {result2}")