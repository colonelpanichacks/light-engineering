#!/usr/bin/env python3
"""
Test script to login and check the player positions API
"""
import requests
import json

# Create a session to maintain cookies
session = requests.Session()

# Login
login_url = "http://localhost:8420/login"
login_data = {
    "username": "coach_dad",
    "password": "changeme"
}

print("Attempting to login...")
response = session.post(login_url, data=login_data, allow_redirects=False)
print(f"Login response status: {response.status_code}")
print(f"Login response headers: {dict(response.headers)}")

if response.status_code in [302, 303]:
    print("Login successful (redirect detected)")
    
    # Now try to access the player positions API
    print("\nFetching player positions...")
    api_url = "http://localhost:8420/api/player_positions"
    api_response = session.get(api_url)
    print(f"API response status: {api_response.status_code}")
    
    if api_response.status_code == 200:
        data = api_response.json()
        print(f"\nAPI Response:")
        print(json.dumps(data, indent=2))
        
        if "players" in data:
            if len(data["players"]) == 0:
                print("\n✓ No players online - sidebar would show 'No players online'")
            else:
                print(f"\n✓ {len(data['players'])} player(s) found:")
                for player in data["players"]:
                    print(f"  - {player['name']}: ({player['x']}, {player['y']}, {player['z']}) in {player['dimension']}")
    else:
        print(f"Failed to fetch API data: {api_response.text}")
        
    # Try to fetch the main page to check for JavaScript errors
    print("\nFetching main page...")
    main_response = session.get("http://localhost:8420/")
    print(f"Main page status: {main_response.status_code}")
    
    if main_response.status_code == 200:
        # Check if the page contains the expected elements
        if "mapPlayerList" in main_response.text:
            print("✓ Map player list element found in HTML")
        if "Waiting for data..." in main_response.text:
            print("✓ Default 'Waiting for data...' text found in HTML")
        if "refreshMapData" in main_response.text:
            print("✓ refreshMapData function found in JavaScript")
            
else:
    print(f"Login failed with status: {response.status_code}")
    print(f"Response: {response.text[:500]}")
