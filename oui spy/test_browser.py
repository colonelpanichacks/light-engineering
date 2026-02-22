#!/usr/bin/env python3
"""
Test script to open browser, login, and check Map tab
"""
from playwright.sync_api import sync_playwright
import time

def test_map_tab():
    with sync_playwright() as p:
        # Launch browser
        browser = p.chromium.launch(headless=False)  # Set to False to see the browser
        context = browser.new_context()
        page = context.new_page()
        
        # Collect console messages
        console_messages = []
        errors = []
        
        def handle_console(msg):
            console_messages.append({
                'type': msg.type,
                'text': msg.text,
                'location': msg.location
            })
            print(f"[CONSOLE {msg.type.upper()}] {msg.text}")
            
        def handle_page_error(error):
            errors.append(str(error))
            print(f"[PAGE ERROR] {error}")
        
        page.on("console", handle_console)
        page.on("pageerror", handle_page_error)
        
        try:
            # Navigate to login page
            print("Navigating to http://localhost:8420/login")
            page.goto("http://localhost:8420/login", wait_until="networkidle")
            time.sleep(1)
            
            # Fill in login form
            print("Filling login form...")
            page.fill('input[name="username"]', 'coach_dad')
            page.fill('input[name="password"]', 'changeme')
            
            # Click login button
            print("Clicking login button...")
            page.click('button[type="submit"]')
            
            # Wait for navigation to complete
            page.wait_for_url("http://localhost:8420/", timeout=5000)
            print("Login successful, now on main page")
            time.sleep(2)
            
            # Click on the Map tab
            print("\nLooking for Map tab...")
            # Try different selectors for the Map tab
            map_tab_selectors = [
                'text="Map"',
                '.tab:has-text("Map")',
                'button:has-text("Map")',
                '[data-panel="map"]',
                '.nav-btn:has-text("Map")'
            ]
            
            map_tab_clicked = False
            for selector in map_tab_selectors:
                try:
                    if page.locator(selector).count() > 0:
                        print(f"Found Map tab with selector: {selector}")
                        page.click(selector)
                        map_tab_clicked = True
                        break
                except:
                    continue
            
            if not map_tab_clicked:
                print("Could not find Map tab, trying to inspect page structure...")
                # Get all buttons/tabs
                buttons = page.locator('button').all()
                print(f"Found {len(buttons)} buttons on page")
                for i, btn in enumerate(buttons[:20]):  # Check first 20 buttons
                    try:
                        text = btn.inner_text()
                        if 'map' in text.lower():
                            print(f"Found button {i} with text: {text}")
                            btn.click()
                            map_tab_clicked = True
                            break
                    except:
                        continue
            
            if map_tab_clicked:
                print("Clicked Map tab, waiting for content to load...")
                time.sleep(3)
                
                # Check the player sidebar
                print("\nChecking player sidebar...")
                try:
                    sidebar = page.locator('#mapPlayerList')
                    if sidebar.count() > 0:
                        sidebar_content = sidebar.inner_text()
                        print(f"Sidebar content:\n{sidebar_content}")
                        
                        if "Waiting for data..." in sidebar_content:
                            print("\n⚠️  Sidebar shows 'Waiting for data...'")
                        elif "No players online" in sidebar_content:
                            print("\n✓ Sidebar shows 'No players online'")
                        else:
                            print("\n✓ Sidebar shows player data")
                    else:
                        print("Could not find #mapPlayerList element")
                except Exception as e:
                    print(f"Error checking sidebar: {e}")
                
                # Take a screenshot
                page.screenshot(path="/Users/colonelpanic/Desktop/Projects/oui spy/map_tab_screenshot.png")
                print("\nScreenshot saved to map_tab_screenshot.png")
            else:
                print("Could not click Map tab")
            
            # Wait a bit more to collect any async console messages
            time.sleep(2)
            
            # Summary
            print("\n" + "="*60)
            print("SUMMARY")
            print("="*60)
            print(f"\nTotal console messages: {len(console_messages)}")
            print(f"Total page errors: {len(errors)}")
            
            if errors:
                print("\n❌ JavaScript Errors Found:")
                for error in errors:
                    print(f"  - {error}")
            else:
                print("\n✓ No JavaScript page errors detected")
            
            # Show console errors and warnings
            console_errors = [m for m in console_messages if m['type'] in ['error', 'warning']]
            if console_errors:
                print(f"\n⚠️  Console Errors/Warnings ({len(console_errors)}):")
                for msg in console_errors:
                    print(f"  [{msg['type'].upper()}] {msg['text']}")
            else:
                print("\n✓ No console errors or warnings")
            
            # Keep browser open for a moment
            print("\nKeeping browser open for 5 seconds...")
            time.sleep(5)
            
        except Exception as e:
            print(f"\n❌ Error during test: {e}")
            import traceback
            traceback.print_exc()
        finally:
            browser.close()

if __name__ == "__main__":
    test_map_tab()
