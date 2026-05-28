"""
Diagnostic script to map every visible step on 50kloans.com
The form lives inside an iframe.global iframe.
Fills in test data at each step to advance through the whole form.
Run: python diagnose_50k.py
"""
import time, json, re, random, string
from playwright.sync_api import sync_playwright, Frame
from utils.proxy_manager import ProxyManager
from utils.stealth import inject_stealth

URL = "https://50kloans.com/"

# ── Test data ──────────────────────────────────────────────────────────────────
_uid = str(int(time.time()))[-7:]
TEST = {
    "email":         f"jsmith{_uid}@gmail.com",
    "loan_amount":   "5000",
    "first_name":    "John",
    "last_name":     "Doe",
    "phone":         "5125551234",
    "street":        "123 Main St",
    "city":          "Austin",
    "state":         "TX",
    "zip":           "78701",
    "dob_month":     "01",
    "dob_day":       "15",
    "dob_year":      "1985",
    "ssn":           "123-45-6789",
    "dl_number":     "12345678",
    "dl_state":      "TX",
    "employer":      "Tech Corp",
    "job_title":     "Manager",
    "employer_phone":"5125559876",
    "monthly_income":"3500",
    "pay_frequency": "biweekly",
    "credit_score":  "fair",
    "loan_purpose":  "debt",
    "bank_name":     "Chase",
    "account_type":  "checking",
    "routing":       "021000021",
    "account":       "123456789",
}

SNAP_JS = """
() => {
    function pct() {
        var fills = document.querySelectorAll('[class*="fill"],[class*="Fill"],[class*="bar--value"],[class*="barValue"]');
        for (var i=0; i<fills.length; i++) {
            var w = fills[i].style.width;
            if (w && w.includes('%') && w !== '0%') return w;
        }
        var texts = document.querySelectorAll('[class*="progress"],[class*="Progress"],[class*="step"],[class*="Step"]');
        for (var i=0; i<texts.length; i++) {
            var t = texts[i].textContent.trim();
            if (/^\\d+\\/\\d+$/.test(t) || /^\\d+%$/.test(t)) return t;
        }
        return '';
    }
    function title() {
        var candidates = document.querySelectorAll('h1,h2,h3,[class*="title"],[class*="Title"],[class*="heading"],[class*="question"],[class*="Question"]');
        for (var i=0; i<candidates.length; i++) {
            var t = candidates[i].textContent.trim();
            if (t.length > 3 && t.length < 150) return t;
        }
        return '';
    }
    function inputs() {
        return Array.from(document.querySelectorAll('input:not([type=hidden]),select,textarea'))
            .filter(function(e) { return e.offsetParent !== null; })
            .map(function(e) {
                return {
                    tag: e.tagName.toLowerCase(),
                    type: e.type || '',
                    name: e.name || '',
                    id: e.id || '',
                    placeholder: e.placeholder || '',
                    classes: e.className || ''
                };
            });
    }
    function labels() {
        return Array.from(document.querySelectorAll('label,[class*="label"],[class*="Label"]'))
            .filter(function(e) { return e.offsetParent !== null; })
            .map(function(e) { return e.textContent.trim().substring(0,80); })
            .filter(function(t) { return t && t.length > 1; }).slice(0,15);
    }
    function buttons() {
        return Array.from(document.querySelectorAll('button,input[type=button],input[type=submit]'))
            .filter(function(e) { return e.offsetParent !== null; })
            .map(function(e) { return (e.textContent || e.value || '').trim().substring(0,60); })
            .filter(function(t) { return t && t.length > 1; }).slice(0,12);
    }
    function radios() {
        var r = Array.from(document.querySelectorAll('input[type=radio]'))
            .filter(function(e) { return e.offsetParent !== null; });
        var g = {};
        r.forEach(function(x) { var k=x.name||'?'; if(!g[k])g[k]=[]; g[k].push(x.value); });
        return g;
    }
    function selectOptions() {
        var s = {};
        Array.from(document.querySelectorAll('select'))
            .filter(function(e){return e.offsetParent!==null;})
            .forEach(function(el){
                var k=el.name||el.id||'?';
                s[k]=Array.from(el.options).map(function(o){return o.value+'|'+o.text;});
            });
        return s;
    }
    function chips() {
        var c = Array.from(document.querySelectorAll('[class*="chip"],[class*="Chip"],[class*="option"],[class*="Option"],[class*="choice"],[class*="Choice"]'))
            .filter(function(e){return e.offsetParent!==null;})
            .map(function(e){return e.textContent.trim().substring(0,60);})
            .filter(function(t){return t&&t.length>1;});
        return [...new Set(c)];
    }
    return {
        pct:pct(), title:title(), inputs:inputs(), labels:labels(),
        buttons:buttons(), radios:radios(), selectOptions:selectOptions(), chips:chips(),
        url:location.href
    };
}
"""

def get_frame(page) -> Frame:
    for f in page.frames:
        if 'iframe.global' in f.url:
            return f
    return page.main_frame

def snap(frame):
    return frame.evaluate(SNAP_JS)

def fill(frame, selector, value):
    try:
        el = frame.locator(selector).first
        el.fill(value, timeout=5000)
        return True
    except Exception as e:
        print(f"    [fill ERROR] {selector!r}: {e}")
        return False

def click_btn(frame, text_upper):
    """Click a visible button containing text_upper (case-insensitive)."""
    try:
        # First try: wait for an enabled button matching the text
        for attempt in range(3):
            btns = frame.locator("button, input[type=submit]").all()
            for b in btns:
                t = (b.text_content() or '').strip().upper()
                if text_upper in t:
                    if b.is_enabled():
                        b.click(timeout=5000)
                        return True
            if attempt < 2:
                time.sleep(0.8)  # wait for button to become enabled
    except Exception as e:
        print(f"    [click_btn ERROR] {text_upper!r}: {e}")
    return False

def click_chip(frame, text_fragment):
    """Click the first chip whose text matches text_fragment (case-insensitive)."""
    frag = text_fragment.upper()
    try:
        for sel in ['button', '[class*="chip"]', '[class*="option"]', '[class*="choice"]']:
            els = frame.locator(sel).all()
            for el in els:
                t = (el.text_content() or '').strip().upper()
                if frag in t and el.is_visible():
                    el.click(timeout=5000)
                    return t
    except Exception as e:
        print(f"    [click_chip ERROR] {text_fragment!r}: {e}")
    return None

def click_continue(frame):
    for kw in ['CONTINUE', 'NEXT', 'SUBMIT', 'APPLY NOW', 'APPLY', 'GET STARTED', 'REQUEST CASH']:
        if click_btn(frame, kw):
            return kw
    return None

def select_option(frame, selector, value):
    try:
        frame.locator(selector).first.select_option(value=value, timeout=5000)
        return True
    except Exception:
        try:
            frame.locator(selector).first.select_option(label=value, timeout=3000)
            return True
        except Exception as e:
            print(f"    [select ERROR] {selector!r} {value!r}: {e}")
            return False

def handle_step(frame, s, step_num):
    """Fill in the current step and return click result."""
    title = s['title'].lower()
    inputs = s['inputs']
    inp_names = [x['name'] or x['id'] or x['placeholder'] for x in inputs]
    print(f"    Handling: {title!r}")

    # ── Step 0: Loan amount chips ──────────────────────────────────────────────
    if 'how much' in title or 'amount' in title:
        r = click_chip(frame, '$5,000') or click_chip(frame, '5,000') or click_chip(frame, '$1,000')
        print(f"    → chip: {r}")
        return r

    # ── Email ──────────────────────────────────────────────────────────────────
    if 'email' in title:
        fill(frame, 'input[type=email], input[name*=email i], input[id*=email i]', TEST['email'])
        return click_continue(frame)

    # ── First / Last name ─────────────────────────────────────────────────────
    if 'first' in title and 'name' in title:
        fill(frame, 'input[type=text]:visible', TEST['first_name'])
        return click_continue(frame)
    if 'last' in title and 'name' in title:
        fill(frame, 'input[type=text]:visible', TEST['last_name'])
        return click_continue(frame)
    if 'name' in title:
        # might have both first and last
        inp_list = [x for x in inputs if x['type'] in ('text','') and x['tag']=='input']
        if len(inp_list) >= 2:
            sel0 = f'input[name="{inp_list[0]["name"]}"]' if inp_list[0]['name'] else 'input[type=text]:visible >> nth=0'
            sel1 = f'input[name="{inp_list[1]["name"]}"]' if inp_list[1]['name'] else 'input[type=text]:visible >> nth=1'
            fill(frame, sel0, TEST['first_name'])
            fill(frame, sel1, TEST['last_name'])
        elif inp_list:
            fill(frame, 'input[type=text]:visible', TEST['first_name'])
        return click_continue(frame)

    # ── Phone ─────────────────────────────────────────────────────────────────
    if 'phone' in title:
        fill(frame, 'input[type=tel], input[name*=phone i], input[type=text]:visible', TEST['phone'])
        return click_continue(frame)

    # ── Address / Street ──────────────────────────────────────────────────────
    if 'address' in title or 'street' in title:
        inp_list = [x for x in inputs if x['tag']=='input']
        if len(inp_list) >= 1:
            fill(frame, 'input:visible >> nth=0', TEST['street'])
        if len(inp_list) >= 2:
            fill(frame, 'input:visible >> nth=1', TEST['city'])
        if len(inp_list) >= 3:
            fill(frame, 'input:visible >> nth=2', TEST['zip'])
        # State select
        if s['selectOptions']:
            for sel_name in s['selectOptions']:
                select_option(frame, f'select[name="{sel_name}"], select[id="{sel_name}"], select:visible', TEST['state'])
        return click_continue(frame)

    # ── City ──────────────────────────────────────────────────────────────────
    if 'city' in title:
        fill(frame, 'input:visible', TEST['city'])
        return click_continue(frame)

    # ── State ─────────────────────────────────────────────────────────────────
    if 'state' in title and 'bank' not in title and 'driver' not in title and 'license' not in title:
        if s['selectOptions']:
            sel_name = list(s['selectOptions'].keys())[0]
            select_option(frame, f'select', TEST['state'])
        elif s['chips']:
            click_chip(frame, TEST['state'])
        return click_continue(frame)

    # ── Zip ───────────────────────────────────────────────────────────────────
    if 'zip' in title:
        fill(frame, 'input:visible', TEST['zip'])
        return click_continue(frame)

    # ── Date of birth ─────────────────────────────────────────────────────────
    if 'birth' in title or 'dob' in title or 'born' in title:
        inp_list = [x for x in inputs if x['tag']=='input']
        if s['selectOptions']:
            keys = list(s['selectOptions'].keys())
            for k in keys:
                opts_text = ' '.join(s['selectOptions'][k])
                if 'jan' in opts_text.lower() or 'month' in k.lower():
                    select_option(frame, f'select[name="{k}"]', TEST['dob_month'])
                elif any(str(d) in opts_text for d in range(1,32)):
                    select_option(frame, f'select[name="{k}"]', TEST['dob_day'])
                elif '1900' in opts_text or '1980' in opts_text:
                    select_option(frame, f'select[name="{k}"]', TEST['dob_year'])
        elif len(inp_list) >= 3:
            fill(frame, 'input:visible >> nth=0', TEST['dob_month'])
            fill(frame, 'input:visible >> nth=1', TEST['dob_day'])
            fill(frame, 'input:visible >> nth=2', TEST['dob_year'])
        elif len(inp_list) >= 1:
            fill(frame, 'input:visible', f"{TEST['dob_month']}/{TEST['dob_day']}/{TEST['dob_year']}")
        return click_continue(frame)

    # ── SSN ───────────────────────────────────────────────────────────────────
    if 'social' in title or 'ssn' in title:
        # Use last 4 digits if the step only asks for last 4
        ssn_val = TEST['ssn'][-4:] if 'last' in title or '4' in title else TEST['ssn'].replace('-', '')
        # Target by name first, then fallback
        ssn_sel = 'input[name="last_ssn"]' if any(x['name']=='last_ssn' for x in inputs) else 'input:visible'
        fill(frame, ssn_sel, ssn_val)
        time.sleep(1)  # wait for button to enable
        return click_continue(frame)

    # ── Driver license ────────────────────────────────────────────────────────
    if 'license' in title or 'driver' in title:
        fill(frame, 'input[name="drivers_license_number"], input[name*="license" i], input:visible >> nth=0', TEST['dl_number'])
        if s['selectOptions']:
            select_option(frame, 'select[name="drivers_license_state"], select:visible', TEST['dl_state'])
        time.sleep(1)
        return click_continue(frame)

    # ── Loan purpose ──────────────────────────────────────────────────────────
    if 'purpose' in title or 'reason' in title or 'use' in title:
        # Try chip click first
        for purpose_kw in ['Debt', 'debt', 'Personal', 'Home']:
            r = click_chip(frame, purpose_kw)
            if r: return r
        if s['selectOptions']:
            select_option(frame, 'select:visible', TEST['loan_purpose'])
        return click_continue(frame)

    # ── Unsecured debt question ────────────────────────────────────────────────
    if 'debt' in title or 'unsecured' in title:
        r = click_chip(frame, 'No')
        if r: return r

    # ── Free trial upsell ────────────────────────────────────────────────────
    if 'trial' in title or ('free' in title and 'day' in title):
        # Click Yes - clean advance with no modal; No triggers a blocking overlay
        r = click_chip(frame, 'Yes')
        if r: return r
        return click_continue(frame)

    # ── Credit score ──────────────────────────────────────────────────────────
    if 'credit' in title and ('score' in title or 'rating' in title) and 'trial' not in title:
        for kw in ['Fair', 'Good', '580', '620', '640', '660', '580-619']:
            r = click_chip(frame, kw)
            if r: return r
        if s['selectOptions']:
            select_option(frame, 'select:visible', TEST['credit_score'])
        return click_continue(frame)

    # ── Monthly income amount (must be BEFORE general employment handler) ─────
    if 'monthly' in title or ('gross' in title and 'income' in title):
        fill(frame, 'input[name="monthly_income"], input:visible', TEST['monthly_income'])
        time.sleep(1)
        return click_continue(frame)

    # ── Next pay date ─────────────────────────────────────────────────────────
    if 'next pay' in title or ('next' in title and 'pay' in title and 'date' in title):
        # Click "Next scheduled date" chip to populate the date field
        click_chip(frame, 'Next scheduled')
        time.sleep(1)
        return click_continue(frame)

    # ── Pay frequency ─────────────────────────────────────────────────────────
    if 'often' in title or 'paid' in title or 'frequen' in title:
        for kw in ['Biweekly', 'Bi-Weekly', 'Every 2 Weeks']:
            r = click_chip(frame, kw)
            if r: return r
        if s['selectOptions']:
            select_option(frame, 'select:visible', 'biweekly')
        return click_continue(frame)

    # ── Employment source (chips only, no inputs) ─────────────────────────────
    if ('employ' in title or 'source' in title) and not inputs:
        for kw in ['Employed', 'Full Time', 'Full-Time']:
            r = click_chip(frame, kw)
            if r: return r
        return click_continue(frame)

    # ── Employer name / income text entry ─────────────────────────────────────
    if 'employ' in title or 'employer' in title or 'company' in title or 'work' in title:
        for inp in inputs:
            n = inp['name'].lower()
            if not n:
                continue
            sel = f'input[name="{inp["name"]}"]'
            if 'phone' in n:
                fill(frame, sel, TEST['employer_phone'])
            elif 'job' in n or 'title' in n:
                fill(frame, sel, TEST['job_title'])
            elif 'employer' in n or 'company' in n:
                fill(frame, sel, TEST['employer'])
        time.sleep(1)
        return click_continue(frame)
    # ── Length of bank account ───────────────────────────────────────────────────
    if 'length' in title and ('bank' in title or 'account' in title):
        for kw in ['More than 2', '1-2', '2 year', '1 year']:
            r = click_chip(frame, kw)
            if r: return r
        # Fallback: click the first available chip
        real_chips = [c for c in s['chips'] if c.upper() != 'BACK']
        if real_chips:
            return click_chip(frame, real_chips[0])
        return click_continue(frame)

    # ── Bank account number ───────────────────────────────────────────────────
    if (('account' in title and ('number' in title or 'add' in title)) or 'account number' in title) and 'type' not in title and 'length' not in title:
        fill(frame, 'input[name="bank_account_number"], input[name="account_number"], input[name*="account" i], input:visible', TEST['account'])
        time.sleep(1)
        return click_continue(frame)
    # ── Bank info ─────────────────────────────────────────────────────────────
    if 'bank' in title or 'account' in title or 'routing' in title:
        for inp in inputs:
            n = (inp['name'] + inp['id'] + inp['placeholder']).lower()
            sel = f'input[name="{inp["name"]}"]' if inp['name'] else (f'input[id="{inp["id"]}"]' if inp['id'] else 'input:visible')
            if 'routing' in n: fill(frame, sel, TEST['routing'])
            elif 'account' in n and 'type' not in n: fill(frame, sel, TEST['account'])
            elif 'bank' in n and 'name' in n: fill(frame, sel, TEST['bank_name'])
        if s['selectOptions']:
            for sel_name, opts in s['selectOptions'].items():
                opts_text = ' '.join(opts).lower()
                if 'check' in opts_text or 'saving' in opts_text:
                    select_option(frame, f'select[name="{sel_name}"]', 'checking')
        for kw in ['Checking', 'checking']:
            r = click_chip(frame, kw)
            if r: return r
        time.sleep(1)
        return click_continue(frame)

    # ── Military / Yes-No binary chip steps ──────────────────────────────────
    if 'military' in title or 'veteran' in title:
        r = click_chip(frame, 'No')
        if r: return r

    # ── Source of income (chips only) ────────────────────────────────────────
    if 'source' in title and 'income' in title:
        r = click_chip(frame, 'Employed')
        if r: return r

    # ── Loan purpose ─────────────────────────────────────────────────────────
    if 'purpose' in title or 'reason' in title or 'use' in title:
        for kw in ['Debt', 'Personal', 'Home', 'Medical', 'Auto', 'Business']:
            r = click_chip(frame, kw)
            if r: return r

    # ── Phone ─────────────────────────────────────────────────────────────────
    if 'phone' in title or 'mobile' in title or 'number' in title:
        fill(frame, 'input[type=tel], input[name*=phone i], input:visible', TEST['phone'])
        time.sleep(1)
        return click_continue(frame)

    # ── Generic fallback: if only chips visible (no text inputs), click first non-Back chip ──
    if not inputs and s['chips']:
        # Filter out Back
        real_chips = [c for c in s['chips'] if c.upper() != 'BACK']
        if real_chips:
            r = click_chip(frame, real_chips[0])
            if r: return r

    # ── Generic: try to fill any visible text inputs, then continue ───────────
    for inp in inputs:
        if inp['type'] in ('text', 'tel', 'number', ''):
            sel = f'input[name="{inp["name"]}"]' if inp['name'] else 'input:visible'
            fill(frame, sel, TEST['first_name'])
    time.sleep(0.5)
    return click_continue(frame)

def main():
    with open("proxies.txt") as f:
        proxy_raw = f.readline().strip()

    USE_PROXY = False  # set True to use proxy; False = no proxy (for local exploration)

    with sync_playwright() as pw:
        launch_kw = dict(headless=False, args=['--start-maximized'])
        if USE_PROXY:
            launch_kw['proxy'] = ProxyManager.to_playwright_proxy(proxy_raw)
        browser = pw.chromium.launch(**launch_kw)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = ctx.new_page()
        inject_stealth(page)

        print(f"Opening {URL} ...")
        page.goto(URL, wait_until="networkidle", timeout=60000)
        time.sleep(5)

        print("Waiting for iframe.global frame...")
        for _ in range(20):
            frames = [f for f in page.frames if 'iframe.global' in f.url]
            if frames: break
            time.sleep(1)
        else:
            print("ERROR: iframe not found after 20s")
            print("Frames:", [f.url[:60] for f in page.frames])

        frame = get_frame(page)
        print(f"Frame: {frame.url[:80]}\n")
        time.sleep(3)

        steps = []
        for step_num in range(0, 50):
            try:
                s = snap(frame)
            except Exception as e:
                print(f"Snap failed at step {step_num}: {e}")
                break

            s['step'] = step_num
            steps.append(s)

            print(f"\n{'='*60}")
            print(f"Step {step_num:02d} | progress={s['pct']!r}")
            print(f"  Title   : {s['title']}")
            print(f"  Inputs  : {[(x['name'] or x['id'] or x['placeholder'] or x['type']) + '/' + x['type'] for x in s['inputs']]}")
            print(f"  Labels  : {s['labels'][:8]}")
            print(f"  Buttons : {s['buttons'][:8]}")
            print(f"  Radios  : {s['radios']}")
            print(f"  Chips   : {s['chips'][:10]}")
            print(f"  Selects : {list(s['selectOptions'].keys())}")

            t = s['title'].lower()
            if 'thank' in t or 'congratulation' in t or 'review' in t or ('offer' in t and 'loan' in t) or 'match' in t:
                print(">>> Reached end / thank-you / offer page. Done!")
                break

            result = handle_step(frame, s, step_num)
            print(f"  → action result: {result}")

            if not result:
                print("  >>> No action taken. Stopping.")
                break

            time.sleep(5)

            # Verify advance
            try:
                new_s = snap(frame)
                if new_s['title'] == s['title'] and step_num > 0:
                    print(f"  >>> Stuck on same step (title unchanged). Stopping.")
                    break
            except Exception:
                pass

        print(f"\n{'='*60}")
        print(f"TOTAL STEPS CAPTURED: {len(steps)}")
        with open('/tmp/50k_steps.json', 'w') as f:
            json.dump(steps, f, indent=2)
        print("Saved to /tmp/50k_steps.json")
        input("Press ENTER to close browser...")
        browser.close()

if __name__ == '__main__':
    main()

