import os
import re
import time
import random
import threading
import datetime
import functools
import requests
import html
import json
from flask import Flask, render_template, request, jsonify, Response
from collections import deque
import oci

# ---- Timezone Configuration (User device timezone) ----
from zoneinfo import ZoneInfo, available_timezones

def get_user_tz(tz_name=None):
    if tz_name and tz_name in available_timezones():
        return ZoneInfo(tz_name)
    tz_env = os.environ.get("TZ", "Asia/Jakarta")
    if tz_env in available_timezones():
        return ZoneInfo(tz_env)
    return ZoneInfo("UTC")

def get_user_time(tz_name=None):
    tz = get_user_tz(tz_name)
    return datetime.datetime.now(tz)

def format_user_time(dt=None, tz_name=None):
    if dt is None:
        dt = get_user_time(tz_name)
    return dt.strftime('%Y-%m-%d %H:%M:%S')

# Legacy alias
PHNOM_PENH_TZ = ZoneInfo("Asia/Phnom_Penh")
get_phnom_penh_time = lambda: get_user_time("Asia/Phnom_Penh")
format_phnom_penh_time = lambda dt=None: format_user_time(dt, "Asia/Phnom_Penh")

app = Flask(__name__)

# ---- Security Headers ----
@app.after_request
def add_security_headers(response):
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

# ---- Config ----
ADMIN_PASSWORD = os.environ.get('APP_PASSWORD')
if not ADMIN_PASSWORD:
    print("WARNING: APP_PASSWORD not set. Running WITHOUT authentication. Set APP_PASSWORD to enable Basic Auth.")

MAX_ATTEMPTS = int(os.environ.get('MAX_ATTEMPTS', 1000))
DEFAULT_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '8662502307:AAF_5VwOGGK5tyEoEU6ujqcRLZulcgL9dXU')
DEFAULT_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '5540796673')
# Active Telegram Chat Tracker
tg_chat_lock = threading.Lock()
active_telegram_chat_id = DEFAULT_CHAT_ID

def set_active_chat_id(cid):
    global active_telegram_chat_id
    if cid:
        with tg_chat_lock:
            active_telegram_chat_id = str(cid).strip()

def get_active_chat_id():
    with tg_chat_lock:
        return active_telegram_chat_id or DEFAULT_CHAT_ID


# ---- Shared state ----
global_logs = deque(maxlen=1000)
logs_lock = threading.Lock()

automation_lock = threading.Lock()
automation_running = False
automation_shape = None
stop_event = threading.Event()

active_config_cache = None
active_config_lock = threading.Lock()
loop_stop_reason = None


def save_active_config(data):
    global active_config_cache
    if not data:
        return
    with active_config_lock:
        active_config_cache = data.copy() if isinstance(data, dict) else data
    for cfg_file in ('/app/active_config.json', 'active_config.json'):
        try:
            with open(cfg_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass


def get_saved_config():
    global active_config_cache
    with active_config_lock:
        if active_config_cache and isinstance(active_config_cache, dict) and active_config_cache.get('user') and active_config_cache.get('private_key'):
            return active_config_cache.copy()
    for cfg_file in ('/app/active_config.json', 'active_config.json'):
        if os.path.exists(cfg_file):
            try:
                with open(cfg_file) as f:
                    data = json.load(f)
                if data and isinstance(data, dict) and data.get('user') and data.get('private_key'):
                    with active_config_lock:
                        active_config_cache = data.copy()
                    return data
            except Exception:
                pass
    return None

# Structured automation metrics tracker
automation_stats = {
    "start_time": None,
    "total_attempts": 0,
    "attempts_today": 0,
    "today_date": None,
    "last_attempt_time": None,
    "last_status": "Idle",
    "target_name": "AlwaysFree-ARM",
    "shape": "VM.Standard.A1.Flex",
    "ocpus": 4,
    "memory_gb": 24,
    "boot_volume_gb": 50,
    "region": "ap-singapore-1",
    "ad": "",
    "oci_username": None,
    "capacity_busy_count": 0,
    "rate_limit_count": 0,
    "error_count": 0,
    "success": False,
    "instance_id": None,
    "public_ip": None,
    "daily_reports_sent": set(),
}
stats_lock = threading.Lock()

# Per-request timezone
_user_tz = threading.local()

def set_user_tz(tz_name):
    _user_tz.name = tz_name

def get_current_tz():
    return getattr(_user_tz, 'name', None)

# ---- Telegram live log settings ----
tg_live_lock = threading.Lock()
tg_live_enabled = False
tg_live_bot_token = None
tg_live_chat_id = None
tg_live_last_sent = 0
tg_live_min_interval = 3


def add_log(message):
    tz = get_current_tz()
    timestamp = format_user_time(tz_name=tz)
    line = f"[{timestamp}] {message}"
    print(line, flush=True)
    with logs_lock:
        global_logs.append(line)
    _send_live_log_to_telegram(line)

def _send_live_log_to_telegram(line):
    global tg_live_enabled, tg_live_bot_token, tg_live_chat_id, tg_live_last_sent
    with tg_live_lock:
        if not tg_live_enabled or not tg_live_bot_token or not tg_live_chat_id:
            return
        now = time.time()
        if now - tg_live_last_sent < tg_live_min_interval:
            return
        tg_live_last_sent = now
        token = tg_live_bot_token
        chat_id = tg_live_chat_id
    try:
        clean_msg = line
        if len(clean_msg) > 4000:
            clean_msg = clean_msg[:4000] + "..."
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": f"<code>{html.escape(clean_msg)}</code>",
            "parse_mode": "HTML"
        }
        requests.post(url, json=payload, timeout=5)
    except Exception:
        pass


def generate_daily_report_text(tz_name=None):
    tz = get_user_tz(tz_name or get_current_tz())
    now_dt = datetime.datetime.now(tz)
    with stats_lock:
        st = automation_stats.copy()
    with automation_lock:
        running = automation_running

    state_badge = "🟢 <b>Actively Snatching (24/7)</b>" if running else "⏸️ <b>Idle / Paused</b>"

    uptime_str = "0m"
    if st.get("start_time"):
        elapsed_sec = int(time.time() - st["start_time"])
        days = elapsed_sec // 86400
        hours = (elapsed_sec % 86400) // 3600
        minutes = (elapsed_sec % 3600) // 60
        if days > 0:
            uptime_str = f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            uptime_str = f"{hours}h {minutes}m"
        else:
            uptime_str = f"{minutes}m"

    with logs_lock:
        recent = list(global_logs)[-3:]
    logs_clean = "\n".join(f"• <code>{html.escape(l)}</code>" for l in recent) if recent else "<i>No recent logs</i>"

    specs_str = st.get('specs_mode') or f"{st.get('ocpus', 4)} OCPUs | {st.get('memory_gb', 24)} GB RAM"
    curr_spec_str = st.get('current_spec_label', '4 OCPU / 24 GB')

    report = (
        "📅 <b>Oracle A1 VM Snatcher — Daily Update</b>\n\n"
        f"• <b>Status:</b> {state_badge}\n"
        f"• <b>Target:</b> {html.escape(str(st.get('target_name', 'AlwaysFree-ARM')))}\n"
        f"• <b>Specs:</b> {html.escape(str(specs_str))} (Current: {html.escape(str(curr_spec_str))}) | {st.get('boot_volume_gb', 50)} GB Boot\n"
        f"• <b>Shape:</b> <code>{html.escape(str(st.get('shape', 'VM.Standard.A1.Flex')))}</code>\n"
        f"• <b>Region:</b> {html.escape(str(st.get('region', 'ap-singapore-1')))} ({html.escape(str(st.get('ad', 'AD-1'))[:30])})\n"
        f"• <b>Uptime:</b> {uptime_str}\n"
        f"• <b>Report Time:</b> {now_dt.strftime('%A, %d %B %Y %H:%M:%S (%Z)')}\n\n"
        "📊 <b>24-Hour Snatcher Statistics:</b>\n"
        f"• <b>Total Attempts:</b> {st.get('total_attempts', 0):,} requests\n"
        f"• <b>Attempts Today:</b> {st.get('attempts_today', 0):,} requests\n"
        f"• <b>Out of Capacity (500):</b> {st.get('capacity_busy_count', 0):,}\n"
        f"• <b>Rate Limited (429):</b> {st.get('rate_limit_count', 0):,}\n"
        f"• <b>Latest Result:</b> {html.escape(str(st.get('last_status', 'Pending')))}\n\n"
        f"📋 <b>Recent Activity Snippet:</b>\n{logs_clean}\n\n"
        "💡 <i>The snatcher is actively querying Oracle for freed capacity. The instant a slot opens up, your VM will be claimed and you will receive an immediate alert with your public IP!</i>\n\n"
        "<b>Available Commands:</b>\n"
        "• <code>/status</code> - View current real-time status\n"
        "• <code>/pause</code> - Pause snatcher provisioning loop\n"
        "• <code>/resume</code> - Resume snatcher provisioning loop\n"
        "• <code>/logs</code> - View latest 10 logs (or <code>/logs 20</code>)\n"
        "• <code>/daily</code> - Force send daily update report\n"
        "• <code>/livelog on</code> - Stream all attempts live to Telegram"
    )
    return report


def generate_status_text(tz_name=None):
    tz = get_user_tz(tz_name or get_current_tz())
    now_dt = datetime.datetime.now(tz)
    with stats_lock:
        st = automation_stats.copy()
    with automation_lock:
        running = automation_running

    state_badge = "🟢 <b>Actively Snatching (24/7)</b>" if running else "⏸️ <b>Idle / Paused</b>"

    uptime_str = "0m"
    if st.get("start_time"):
        elapsed_sec = int(time.time() - st["start_time"])
        days = elapsed_sec // 86400
        hours = (elapsed_sec % 86400) // 3600
        minutes = (elapsed_sec % 3600) // 60
        uptime_str = f"{days}d {hours}h {minutes}m" if days > 0 else (f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m")

    with logs_lock:
        recent = list(global_logs)[-3:]
    logs_clean = "\n".join(f"• <code>{html.escape(l)}</code>" for l in recent) if recent else "<i>No logs yet</i>"

    target_specs_str = st.get('specs_mode') or f"{st.get('ocpus', 4)} OCPU, {st.get('memory_gb', 24)} GB RAM"
    current_attempt_str = f" (Attempting: {st.get('current_spec_label')})" if st.get('current_spec_label') else ""
    action_tip = "Tap <b>⏸️ Pause</b> below or send <code>/pause</code> to pause." if running else "Tap <b>▶️ Resume</b> below or send <code>/resume</code> to resume."

    return (
        "🤖 <b>Oracle A1 VM Snatcher Status</b>\n\n"
        f"• <b>State:</b> {state_badge}\n"
        f"• <b>Target:</b> {html.escape(str(st.get('target_name', 'AlwaysFree-ARM')))}\n"
        f"• <b>Shape:</b> <code>{html.escape(str(st.get('shape', 'VM.Standard.A1.Flex')))}</code>\n"
        f"• <b>Specs:</b> {html.escape(str(target_specs_str))}{html.escape(str(current_attempt_str))}\n"
        f"• <b>Region:</b> {html.escape(str(st.get('region', 'ap-singapore-1')))}\n"
        f"• <b>Attempts:</b> {st.get('total_attempts', 0):,} total ({st.get('attempts_today', 0):,} today)\n"
        f"• <b>Uptime:</b> {uptime_str}\n"
        f"• <b>Time:</b> {now_dt.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"<b>Latest Activity:</b>\n{logs_clean}\n\n"
        f"💡 {action_tip}\n"
        "Send <code>/logs</code> for more logs, or <code>/daily</code> for daily statistics report."
    )


def generate_logs_text(num_lines=10):
    with logs_lock:
        logs_list = list(global_logs)
    if not logs_list:
        return "📋 <b>Recent Activity Logs:</b>\n\n<i>No logs recorded yet. Snatcher is initializing...</i>"
    recent = logs_list[-min(num_lines, 30):]
    logs_clean = "\n\n".join(f"• <code>{html.escape(l)}</code>" for l in recent)
    return (
        f"📋 <b>Recent Activity Logs (last {len(recent)} entries):</b>\n\n"
        f"{logs_clean}\n\n"
        "<i>Send /logs 20 for more lines, or /livelog on to stream all attempts.</i>"
    )


def build_config(data):
    return {
        "user": data.get('user'),
        "fingerprint": data.get('fingerprint'),
        "tenancy": data.get('tenancy'),
        "region": data.get('region'),
        "key_content": data.get('private_key')
    }


def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not ADMIN_PASSWORD:
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or auth.password != ADMIN_PASSWORD:
            return Response(
                'Authentication required',
                401,
                {'WWW-Authenticate': 'Basic realm="OCI Provisioner"'}
            )
        return f(*args, **kwargs)
    return decorated


@app.route('/health')
def health():
    return jsonify({'status': 'ok'}), 200


@app.route('/')
def home():
    try:
        return render_template('index.html', default_bot_token=DEFAULT_BOT_TOKEN, default_chat_id=DEFAULT_CHAT_ID)
    except Exception as e:
        return f"Flask Template Error: {str(e)}", 500


@app.route('/api/list-images', methods=['POST'])
@require_auth
def list_available_images():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    config = build_config(data)
    shape = data.get('shape')
    all_os_mode = data.get('all_os_mode', False)

    try:
        oci.config.validate_config(config)
        compute = oci.core.ComputeClient(config)
        kwargs = {'compartment_id': config['tenancy']}
        if shape:
            kwargs['shape'] = shape
        images = compute.list_images(**kwargs).data
        min_dt = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc).astimezone(PHNOM_PENH_TZ)
        images = sorted(
            images,
            key=lambda i: i.time_created.astimezone(PHNOM_PENH_TZ) if i.time_created else min_dt,
            reverse=True
        )
        valid = []
        for img in images:
            if getattr(img, 'lifecycle_state', '') != 'AVAILABLE':
                continue
            os_name = (getattr(img, 'operating_system', '') or '').lower()
            version = (getattr(img, 'operating_system_version', '') or '').strip()
            display_name = (img.display_name or '').lower()
            if not all_os_mode:
                if 'ubuntu' not in os_name:
                    continue
                major = 0
                if version:
                    try:
                        major = int(str(version).split('.')[0])
                    except (ValueError, IndexError):
                        major = 0
                else:
                    m = re.search(r'ubuntu[-_\s]?(\d+)', display_name)
                    if m:
                        major = int(m.group(1))
                if major < 18:
                    continue
            valid.append({
                'id': img.id,
                'name': img.display_name or f"{getattr(img, 'operating_system', 'Unknown')} {version}",
                'version': version,
                'os': getattr(img, 'operating_system', 'Unknown'),
                'os_version': version
            })
        return jsonify({'success': True, 'images': valid[:50]})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/list-subnets', methods=['POST'])
@require_auth
def list_available_subnets():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    config = build_config(data)
    try:
        oci.config.validate_config(config)
        network_client = oci.core.VirtualNetworkClient(config)
        identity_client = oci.identity.IdentityClient(config)
        tenancy = config['tenancy']
        ads = identity_client.list_availability_domains(compartment_id=tenancy).data
        vcns = network_client.list_vcns(compartment_id=tenancy).data
        if not vcns:
            return jsonify({'success': False, 'error': 'No VCNs found in this tenancy'})
        all_subnets = []
        for vcn in vcns:
            subnets = network_client.list_subnets(compartment_id=tenancy, vcn_id=vcn.id).data
            for sn in subnets:
                if getattr(sn, 'lifecycle_state', '') != 'AVAILABLE':
                    continue
                all_subnets.append({
                    'id': sn.id,
                    'name': sn.display_name or 'Unnamed',
                    'cidr': sn.cidr_block or 'N/A',
                    'vcn_name': vcn.display_name or 'Unnamed VCN',
                    'vcn_id': vcn.id,
                    'ad': sn.availability_domain or 'Regional',
                    'public': getattr(sn, 'prohibit_public_ip_on_vnic', False) == False,
                    'dns': sn.dns_label or 'N/A'
                })
        return jsonify({'success': True, 'subnets': all_subnets})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/list-shapes', methods=['POST'])
@require_auth
def list_available_shapes():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    config = build_config(data)
    try:
        oci.config.validate_config(config)
        compute_client = oci.core.ComputeClient(config)
        identity_client = oci.identity.IdentityClient(config)
        tenancy = config['tenancy']
        ads = identity_client.list_availability_domains(compartment_id=tenancy).data
        all_shapes = []
        seen = set()
        ad_availability = {}
        for ad in ads:
            try:
                shapes = compute_client.list_shapes(compartment_id=tenancy, availability_domain=ad.name).data
                for shape in shapes:
                    name = shape.shape
                    if name in seen:
                        ad_availability.setdefault(name, []).append(ad.name)
                        continue
                    seen.add(name)
                    ad_availability[name] = [ad.name]
                    all_shapes.append({
                        'name': name,
                        'ocpus': getattr(shape, 'ocpus', None),
                        'memory': getattr(shape, 'memory_in_gbs', None),
                        'processor': getattr(shape, 'processor_description', None),
                        'is_flex': 'Flex' in name,
                        'is_burstable': getattr(shape, 'is_burstable', False),
                        'max_vnic_attachment': getattr(shape, 'networking_bandwidth_in_gbps', None)
                    })
            except Exception:
                continue
        for s in all_shapes:
            s['ads'] = list(set(ad_availability.get(s['name'], [])))
        free_tier = {'VM.Standard.A1.Flex', 'VM.Standard.E2.1.Micro'}
        all_shapes.sort(key=lambda s: (s['name'] not in free_tier, s['name']))
        return jsonify({'success': True, 'shapes': all_shapes, 'ad_count': len(ads)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/create-subnet', methods=['POST'])
@require_auth
def create_subnet():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    config = build_config(data)
    vcn_cidr = data.get('vcn_cidr', '10.0.0.0/16').strip()
    subnet_cidr = data.get('subnet_cidr', '10.0.0.0/24').strip()
    subnet_name = data.get('subnet_name', 'provisioner-subnet').strip()
    vcn_name = data.get('vcn_name', 'provisioner-vcn').strip()
    try:
        oci.config.validate_config(config)
        network_client = oci.core.VirtualNetworkClient(config)
        identity_client = oci.identity.IdentityClient(config)
        tenancy = config['tenancy']
        ads = identity_client.list_availability_domains(compartment_id=tenancy).data
        target_ad = ads[0].name if ads else None
        vcns = network_client.list_vcns(compartment_id=tenancy).data
        vcn = None
        if vcns:
            vcn = vcns[0]
            add_log(f"Using existing VCN: {vcn.display_name} ({vcn.id[:20]}...)")
        else:
            add_log(f"Creating VCN '{vcn_name}' with CIDR {vcn_cidr}...")
            vcn = network_client.create_vcn(
                create_vcn_details=oci.core.models.CreateVcnDetails(
                    compartment_id=tenancy, cidr_block=vcn_cidr,
                    display_name=vcn_name, dns_label='provvcn'
                )
            ).data
            add_log(f"VCN created: {vcn.id[:20]}...")
            time.sleep(2)
        existing_subnets = network_client.list_subnets(compartment_id=tenancy, vcn_id=vcn.id).data
        if existing_subnets:
            subnet = existing_subnets[0]
            return jsonify({
                'success': True, 'created': False, 'message': 'Subnet already exists',
                'subnet': {
                    'id': subnet.id, 'name': subnet.display_name,
                    'cidr': subnet.cidr_block, 'vcn_name': vcn.display_name,
                    'vcn_id': vcn.id, 'ad': subnet.availability_domain or 'Regional',
                    'public': getattr(subnet, 'prohibit_public_ip_on_vnic', False) == False
                }
            })
        igws = network_client.list_internet_gateways(compartment_id=tenancy, vcn_id=vcn.id).data
        igw = None
        for g in igws:
            if getattr(g, 'lifecycle_state', '') == 'AVAILABLE':
                igw = g
                break
        if not igw:
            add_log("Creating Internet Gateway...")
            igw = network_client.create_internet_gateway(
                create_internet_gateway_details=oci.core.models.CreateInternetGatewayDetails(
                    compartment_id=tenancy, vcn_id=vcn.id,
                    display_name='provisioner-igw', is_enabled=True
                )
            ).data
            add_log(f"Internet Gateway created: {igw.id[:20]}...")
            time.sleep(1)
        else:
            add_log(f"Using existing Internet Gateway: {igw.id[:20]}...")
        route_tables = network_client.list_route_tables(compartment_id=tenancy, vcn_id=vcn.id).data
        if route_tables:
            rt = route_tables[0]
            routes = list(getattr(rt, 'route_rules', []))
            has_internet_route = any(
                getattr(r, 'destination', '') == '0.0.0.0/0' and getattr(r, 'network_entity_id', '') == igw.id
                for r in routes
            )
            if not has_internet_route:
                add_log("Adding default route to Internet Gateway...")
                routes.append(oci.core.models.RouteRule(
                    destination='0.0.0.0/0', destination_type='CIDR_BLOCK', network_entity_id=igw.id
                ))
                network_client.update_route_table(
                    rt_id=rt.id,
                    update_route_table_details=oci.core.models.UpdateRouteTableDetails(route_rules=routes)
                )
                add_log("Route table updated.")
        add_log(f"Creating subnet '{subnet_name}' with CIDR {subnet_cidr}...")
        create_subnet_details = oci.core.models.CreateSubnetDetails(
            compartment_id=tenancy, vcn_id=vcn.id, cidr_block=subnet_cidr,
            display_name=subnet_name, dns_label='provsubnet',
            prohibit_public_ip_on_vnic=False
        )
        if target_ad:
            create_subnet_details.availability_domain = target_ad
        subnet = network_client.create_subnet(create_subnet_details=create_subnet_details).data
        add_log(f"Subnet created: {subnet.id[:20]}...")
        for _ in range(10):
            sn = network_client.get_subnet(subnet_id=subnet.id).data
            if getattr(sn, 'lifecycle_state', '') == 'AVAILABLE':
                break
            time.sleep(1)
        return jsonify({
            'success': True, 'created': True, 'message': 'Subnet created successfully',
            'subnet': {
                'id': subnet.id, 'name': subnet.display_name,
                'cidr': subnet.cidr_block, 'vcn_name': vcn.display_name,
                'vcn_id': vcn.id, 'ad': subnet.availability_domain or 'Regional',
                'public': getattr(subnet, 'prohibit_public_ip_on_vnic', False) == False
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/test-launch', methods=['POST'])
@require_auth
def test_launch():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    config = build_config(data)
    try:
        oci.config.validate_config(config)
        compute_client = oci.core.ComputeClient(config)
        network_client = oci.core.VirtualNetworkClient(config)
        identity_client = oci.identity.IdentityClient(config)
        block_client = oci.core.BlockstorageClient(config)
        tenancy = config['tenancy']
        ads = identity_client.list_availability_domains(compartment_id=tenancy).data
        vcns = network_client.list_vcns(compartment_id=tenancy).data
        subnets = []
        if vcns:
            subnets = network_client.list_subnets(compartment_id=tenancy, vcn_id=vcns[0].id).data
        image_id = data.get('image_id')
        shape = data.get('shape')
        subnet_id = data.get('subnet_id')
        image_valid = False
        image_details = None
        if image_id:
            try:
                img = compute_client.get_image(image_id=image_id).data
                image_valid = getattr(img, 'lifecycle_state', '') == 'AVAILABLE'
                image_details = {
                    'display_name': img.display_name,
                    'os': getattr(img, 'operating_system', 'N/A'),
                    'os_version': getattr(img, 'operating_system_version', 'N/A'),
                    'size_in_mbs': getattr(img, 'size_in_mbs', 'N/A'),
                    'lifecycle_state': getattr(img, 'lifecycle_state', 'N/A')
                }
            except Exception as e:
                image_details = {'error': str(e)[:100]}
        subnet_valid = False
        subnet_details = None
        if subnet_id:
            try:
                sn = network_client.get_subnet(subnet_id=subnet_id).data
                subnet_valid = getattr(sn, 'lifecycle_state', '') == 'AVAILABLE'
                subnet_details = {
                    'display_name': sn.display_name,
                    'cidr_block': getattr(sn, 'cidr_block', 'N/A'),
                    'availability_domain': getattr(sn, 'availability_domain', 'Regional'),
                    'prohibit_public_ip': getattr(sn, 'prohibit_public_ip_on_vnic', False),
                    'lifecycle_state': getattr(sn, 'lifecycle_state', 'N/A')
                }
            except Exception as e:
                subnet_details = {'error': str(e)[:100]}
        shape_compat = []
        if image_id:
            try:
                shapes = compute_client.list_image_shape_compatibility_entries(image_id=image_id).data
                shape_compat = [s.shape for s in shapes]
            except Exception as e:
                shape_compat = ['Error: ' + str(e)[:80]]
        ok, err = check_free_tier_limits(config, data, compute_client, block_client, identity_client)
        return jsonify({
            'success': True,
            'debug': {
                'region': config.get('region'),
                'ad': ads[0].name if ads else 'N/A',
                'ads_available': [ad.name for ad in ads],
                'vcns_found': len(vcns),
                'subnets_found': len(subnets),
                'image_valid': image_valid,
                'image_details': image_details,
                'subnet_valid': subnet_valid,
                'subnet_details': subnet_details,
                'shape': shape,
                'shape_compatible_with_image': shape_compat,
                'free_tier_ok': ok,
                'free_tier_error': err
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/list-vnics', methods=['POST'])
@require_auth
def list_vnics():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    config = build_config(data)
    target_subnet_id = data.get('subnet_id', '').strip() or None
    try:
        oci.config.validate_config(config)
        compute_client = oci.core.ComputeClient(config)
        network_client = oci.core.VirtualNetworkClient(config)
        tenancy = config['tenancy']
        vcns = {v.id: v for v in network_client.list_vcns(compartment_id=tenancy).data}
        subnets = {s.id: s for s in network_client.list_subnets(compartment_id=tenancy).data}
        instances = {i.id: i for i in compute_client.list_instances(compartment_id=tenancy).data}
        vnics = []
        vnic_attachments = compute_client.list_vnic_attachments(compartment_id=tenancy).data
        for att in vnic_attachments:
            try:
                vnic = network_client.get_vnic(vnic_id=att.vnic_id).data
                subnet = subnets.get(vnic.subnet_id)
                vcn = vcns.get(subnet.vcn_id) if subnet else None
                if target_subnet_id and vnic.subnet_id != target_subnet_id:
                    continue
                instance = instances.get(att.instance_id)
                vnics.append({
                    'id': vnic.id, 'display_name': vnic.display_name or 'Unnamed',
                    'private_ip': vnic.private_ip, 'public_ip': vnic.public_ip or 'None',
                    'subnet_id': vnic.subnet_id, 'subnet_name': subnet.display_name if subnet else 'Unknown',
                    'vcn_id': vcn.id if vcn else 'Unknown', 'vcn_name': vcn.display_name if vcn else 'Unknown',
                    'lifecycle_state': vnic.lifecycle_state, 'is_primary': getattr(att, 'is_primary', False),
                    'instance_id': att.instance_id, 'instance_name': instance.display_name if instance else 'Unknown'
                })
            except Exception:
                pass
        vcn_list = [{'id': v.id, 'name': v.display_name or 'Unnamed'} for v in vcns.values()]
        return jsonify({'success': True, 'vnics': vnics, 'vcns': vcn_list, 'filtered_by_subnet': target_subnet_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/open-firewall', methods=['POST'])
@require_auth
def open_firewall():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    config = build_config(data)
    subnet_id = data.get('subnet_id')
    ports = data.get('ports', 'all')
    cidr = data.get('cidr', '0.0.0.0/0')
    direction = data.get('direction', 'ingress')
    if not subnet_id:
        return jsonify({'success': False, 'error': 'subnet_id required'})
    try:
        oci.config.validate_config(config)
        network_client = oci.core.VirtualNetworkClient(config)
        subnet = network_client.get_subnet(subnet_id=subnet_id).data
        port_list = []
        if ports == 'all' or ports == '*':
            port_list = ['all']
        else:
            port_list = [p.strip() for p in str(ports).split(',') if p.strip()]
        directions_to_add = []
        if direction in ('ingress', 'both'):
            directions_to_add.append('INGRESS')
        if direction in ('egress', 'both'):
            directions_to_add.append('EGRESS')
        nsg_ids = getattr(subnet, 'network_security_group_ids', [])
        if nsg_ids and len(nsg_ids) > 0:
            rules = []
            for dir in directions_to_add:
                for port in port_list:
                    if port == 'all':
                        rules.append(oci.core.models.AddSecurityRuleDetails(
                            direction=dir, protocol='all',
                            source=cidr if dir == 'INGRESS' else None,
                            destination=cidr if dir == 'EGRESS' else None,
                            description='OCI Provisioner: ' + dir.lower() + ' all traffic'
                        ))
                    else:
                        rules.append(oci.core.models.AddSecurityRuleDetails(
                            direction=dir, protocol='6',
                            source=cidr if dir == 'INGRESS' else None,
                            destination=cidr if dir == 'EGRESS' else None,
                            tcp_options=oci.core.models.TcpOptions(
                                destination_port_range=oci.core.models.PortRange(min=int(port), max=int(port))
                            ),
                            description='OCI Provisioner: ' + dir.lower() + ' port ' + port
                        ))
            result = network_client.add_network_security_group_security_rules(
                network_security_group_id=nsg_ids[0],
                add_network_security_group_security_rules_details=oci.core.models.AddNetworkSecurityGroupSecurityRulesDetails(
                    security_rules=rules
                )
            )
            return jsonify({
                'success': True, 'method': 'NSG', 'nsg_id': nsg_ids[0],
                'rules_added': len(result.data.security_rules),
                'ports': ports, 'cidr': cidr, 'direction': direction
            })
        sec_list_ids = getattr(subnet, 'security_list_ids', [])
        if not sec_list_ids:
            return jsonify({'success': False, 'error': 'No security list or NSG found on subnet'})
        sec_list = network_client.get_security_list(security_list_id=sec_list_ids[0]).data
        new_ingress = list(getattr(sec_list, 'ingress_security_rules', []))
        new_egress = list(getattr(sec_list, 'egress_security_rules', []))
        added = []
        for dir in directions_to_add:
            existing = new_ingress if dir == 'INGRESS' else new_egress
            for port in port_list:
                if port == 'all':
                    already = any(getattr(r, 'source' if dir == 'INGRESS' else 'destination', '') == cidr and getattr(r, 'protocol', '') == 'all' for r in existing)
                    if not already:
                        rule = oci.core.models.IngressSecurityRule(
                            source=cidr, protocol='all', is_stateless=False,
                            description='OCI Provisioner: ' + dir.lower() + ' all traffic'
                        ) if dir == 'INGRESS' else oci.core.models.EgressSecurityRule(
                            destination=cidr, protocol='all', is_stateless=False,
                            description='OCI Provisioner: ' + dir.lower() + ' all traffic'
                        )
                        existing.append(rule)
                        added.append(dir.lower() + ':all')
                else:
                    already = any(
                        getattr(r, 'source' if dir == 'INGRESS' else 'destination', '') == cidr and
                        getattr(r, 'protocol', '') == '6' and
                        getattr(getattr(r, 'tcp_options', None), 'destination_port_range', None) and
                        getattr(getattr(r, 'tcp_options', None), 'destination_port_range').min == int(port)
                        for r in existing
                    )
                    if not already:
                        rule = oci.core.models.IngressSecurityRule(
                            source=cidr, protocol='6', is_stateless=False,
                            tcp_options=oci.core.models.TcpOptions(
                                destination_port_range=oci.core.models.PortRange(min=int(port), max=int(port))
                            ),
                            description='OCI Provisioner: ' + dir.lower() + ' port ' + port
                        ) if dir == 'INGRESS' else oci.core.models.EgressSecurityRule(
                            destination=cidr, protocol='6', is_stateless=False,
                            tcp_options=oci.core.models.TcpOptions(
                                destination_port_range=oci.core.models.PortRange(min=int(port), max=int(port))
                            ),
                            description='OCI Provisioner: ' + dir.lower() + ' port ' + port
                        )
                        existing.append(rule)
                        added.append(dir.lower() + ':' + port)
        if not added:
            return jsonify({'success': True, 'already_open': True, 'message': 'Rule(s) already exist', 'ports': ports, 'cidr': cidr, 'direction': direction})
        network_client.update_security_list(
            security_list_id=sec_list_ids[0],
            update_security_list_details=oci.core.models.UpdateSecurityListDetails(
                ingress_security_rules=new_ingress, egress_security_rules=new_egress
            )
        )
        return jsonify({
            'success': True, 'method': 'SecurityList', 'sec_list_id': sec_list_ids[0],
            'rules_added': len(added), 'ports_added': added,
            'ports': ports, 'cidr': cidr, 'direction': direction
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/scan-security-rules', methods=['POST'])
@require_auth
def scan_security_rules():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    config = build_config(data)
    subnet_id = data.get('subnet_id')
    if not subnet_id:
        return jsonify({'success': False, 'error': 'subnet_id required'})
    try:
        oci.config.validate_config(config)
        network_client = oci.core.VirtualNetworkClient(config)
        subnet = network_client.get_subnet(subnet_id=subnet_id).data
        rules = []
        nsg_ids = getattr(subnet, 'network_security_group_ids', [])
        for nsg_id in nsg_ids:
            nsg = network_client.get_network_security_group(network_security_group_id=nsg_id).data
            nsg_rules = network_client.list_network_security_group_security_rules(network_security_group_id=nsg_id).data
            for r in nsg_rules:
                rules.append({
                    'type': 'NSG', 'direction': r.direction, 'protocol': r.protocol,
                    'source': getattr(r, 'source', 'N/A'), 'destination': getattr(r, 'destination', 'N/A'),
                    'description': getattr(r, 'description', '')
                })
        sec_list_ids = getattr(subnet, 'security_list_ids', [])
        for sec_id in sec_list_ids:
            sec_list = network_client.get_security_list(security_list_id=sec_id).data
            for r in getattr(sec_list, 'ingress_security_rules', []):
                tcp_opts = getattr(r, 'tcp_options', None)
                port_range = None
                if tcp_opts and getattr(tcp_opts, 'destination_port_range', None):
                    port_range = str(tcp_opts.destination_port_range.min)
                    if tcp_opts.destination_port_range.max != tcp_opts.destination_port_range.min:
                        port_range += '-' + str(tcp_opts.destination_port_range.max)
                rules.append({
                    'type': 'SecurityList', 'direction': 'INGRESS',
                    'protocol': getattr(r, 'protocol', 'N/A'), 'source': getattr(r, 'source', 'N/A'),
                    'destination': 'N/A', 'port_range': port_range,
                    'description': getattr(r, 'description', '')
                })
            for r in getattr(sec_list, 'egress_security_rules', []):
                tcp_opts = getattr(r, 'tcp_options', None)
                port_range = None
                if tcp_opts and getattr(tcp_opts, 'destination_port_range', None):
                    port_range = str(tcp_opts.destination_port_range.min)
                    if tcp_opts.destination_port_range.max != tcp_opts.destination_port_range.min:
                        port_range += '-' + str(tcp_opts.destination_port_range.max)
                rules.append({
                    'type': 'SecurityList', 'direction': 'EGRESS',
                    'protocol': getattr(r, 'protocol', 'N/A'), 'source': 'N/A',
                    'destination': getattr(r, 'destination', 'N/A'), 'port_range': port_range,
                    'description': getattr(r, 'description', '')
                })
        return jsonify({'success': True, 'rules': rules, 'nsg_count': len(nsg_ids), 'sec_list_count': len(sec_list_ids)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


def check_free_tier_limits(config, account_config, compute_client, block_client, identity_client):
    tenancy = config['tenancy']
    requested_shape = account_config.get('shape')
    requested_boot_gb = int(account_config.get('boot_volume_gb', 50))
    if requested_boot_gb < 50:
        requested_boot_gb = 50
    ads = identity_client.list_availability_domains(compartment_id=tenancy).data
    total_storage = 0
    for ad in ads:
        boot_volumes = block_client.list_boot_volumes(compartment_id=tenancy, availability_domain=ad.name).data
        total_storage += sum(int(v.size_in_gbs) for v in boot_volumes if v.lifecycle_state != 'TERMINATED')
    if total_storage + requested_boot_gb > 200:
        return False, f"Storage would exceed 200 GB free tier limit (used {total_storage} GB + requested {requested_boot_gb} GB)"
    instances = compute_client.list_instances(compartment_id=tenancy).data
    if requested_shape == 'VM.Standard.E2.1.Micro':
        micro_count = sum(1 for inst in instances if inst.shape == 'VM.Standard.E2.1.Micro' and inst.lifecycle_state != 'TERMINATED')
        if micro_count >= 2:
            return False, f"Free tier allows only 2 Micro instances (found {micro_count})"
        return True, ""
    if requested_shape == 'VM.Standard.A1.Flex':
        requested_ocpus = int(account_config.get('ocpus', 4))
        requested_memory = int(account_config.get('memory', 24))
        total_ocpus = 0
        total_memory = 0
        for inst in instances:
            if inst.shape == 'VM.Standard.A1.Flex' and inst.lifecycle_state != 'TERMINATED':
                cfg = inst.shape_config
                if cfg:
                    total_ocpus += int(cfg.ocpus or 0)
                    total_memory += int(cfg.memory_in_gbs or 0)
        if total_ocpus + requested_ocpus > 4:
            return False, f"A1 OCPUs would exceed 4 (used {total_ocpus} + requested {requested_ocpus})"
        if total_memory + requested_memory > 24:
            return False, f"A1 memory would exceed 24 GB (used {total_memory} + requested {requested_memory})"
        return True, ""
    return True, ""


def get_instance_public_ip(config, compute_client, network_client, instance_id):
    try:
        for _ in range(30):
            inst = compute_client.get_instance(instance_id=instance_id).data
            if inst.lifecycle_state == 'RUNNING':
                break
            if inst.lifecycle_state in ('TERMINATED', 'TERMINATING'):
                return None, 'Instance terminated'
            time.sleep(2)
        attachments = compute_client.list_vnic_attachments(compartment_id=config['tenancy'], instance_id=instance_id).data
        for att in attachments:
            if getattr(att, 'lifecycle_state', '') == 'ATTACHED':
                vnic = network_client.get_vnic(vnic_id=att.vnic_id).data
                if vnic.public_ip:
                    return vnic.public_ip, None
        return None, 'No public IP assigned'
    except Exception as e:
        return None, str(e)


def list_all_instances(config, compute_client, identity_client, network_client=None):
    tenancy = config['tenancy']
    instances = compute_client.list_instances(compartment_id=tenancy).data
    result = []
    # Create network client if not provided
    if network_client is None:
        network_client = oci.core.VirtualNetworkClient(config)
    for inst in instances:
        if inst.lifecycle_state in ('TERMINATED', 'TERMINATING'):
            continue
        shape = inst.shape
        ocpus = None
        memory = None
        public_ip = None
        if hasattr(inst, 'shape_config') and inst.shape_config:
            ocpus = inst.shape_config.ocpus
            memory = inst.shape_config.memory_in_gbs
        # Try to get public IP from VNIC attachments
        try:
            attachments = compute_client.list_vnic_attachments(
                compartment_id=tenancy, instance_id=inst.id
            ).data
            for att in attachments:
                if getattr(att, 'lifecycle_state', '') == 'ATTACHED':
                    vnic = network_client.get_vnic(vnic_id=att.vnic_id).data
                    if vnic.public_ip:
                        public_ip = vnic.public_ip
                        break
        except Exception:
            pass
        result.append({
            'id': inst.id, 'name': inst.display_name, 'shape': shape,
            'state': inst.lifecycle_state, 'ocpus': ocpus, 'memory': memory,
            'public_ip': public_ip,
            'time_created': inst.time_created.isoformat() if inst.time_created else None,
            'availability_domain': inst.availability_domain
        })
    return result


def terminate_instance(compute_client, instance_id):
    try:
        compute_client.terminate_instance(instance_id=instance_id)
        return True, None
    except Exception as e:
        return False, str(e)


def get_free_tier_usage(config, compute_client, block_client, identity_client):
    tenancy = config['tenancy']
    network_client = oci.core.VirtualNetworkClient(config)
    ads = identity_client.list_availability_domains(compartment_id=tenancy).data
    total_storage = 0
    for ad in ads:
        boot_volumes = block_client.list_boot_volumes(compartment_id=tenancy, availability_domain=ad.name).data
        total_storage += sum(int(v.size_in_gbs) for v in boot_volumes if v.lifecycle_state != 'TERMINATED')
    storage_remaining = max(0, 200 - total_storage)
    instances = compute_client.list_instances(compartment_id=tenancy).data
    micro_count = sum(1 for inst in instances if inst.shape == 'VM.Standard.E2.1.Micro' and inst.lifecycle_state != 'TERMINATED')
    micro_remaining = max(0, 2 - micro_count)
    total_ocpus = 0
    total_memory = 0
    arm_instances = []
    for inst in instances:
        if inst.shape == 'VM.Standard.A1.Flex' and inst.lifecycle_state != 'TERMINATED':
            cfg = inst.shape_config
            if cfg:
                ocpus = int(cfg.ocpus or 0)
                memory = int(cfg.memory_in_gbs or 0)
                total_ocpus += ocpus
                total_memory += memory
                arm_instances.append({'name': inst.display_name, 'ocpus': ocpus, 'memory': memory, 'state': inst.lifecycle_state})
    ocpus_remaining = max(0, 4 - total_ocpus)
    memory_remaining = max(0, 24 - total_memory)
    all_instances = list_all_instances(config, compute_client, identity_client, network_client)
    return {
        'storage': {'used_gb': total_storage, 'limit_gb': 200, 'remaining_gb': storage_remaining, 'percent': round((total_storage / 200) * 100, 1) if total_storage > 0 else 0},
        'micro': {'used': micro_count, 'limit': 2, 'remaining': micro_remaining, 'percent': round((micro_count / 2) * 100, 1) if micro_count > 0 else 0},
        'arm': {'used_ocpus': total_ocpus, 'limit_ocpus': 4, 'remaining_ocpus': ocpus_remaining, 'used_memory_gb': total_memory, 'limit_memory_gb': 24, 'remaining_memory_gb': memory_remaining, 'instances': arm_instances, 'ocpu_percent': round((total_ocpus / 4) * 100, 1) if total_ocpus > 0 else 0, 'memory_percent': round((total_memory / 24) * 100, 1) if total_memory > 0 else 0},
        'all_instances': all_instances
    }


def resolve_telegram_chat(bot_token=None, explicit_chat_id=None):
    token = (bot_token or DEFAULT_BOT_TOKEN or '').strip()
    if explicit_chat_id and str(explicit_chat_id).strip() and str(explicit_chat_id).strip().lower() != 'auto':
        return str(explicit_chat_id).strip(), "Configured Chat"
    if not token:
        return None, None
    try:
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        resp = requests.get(url, timeout=10).json()
        if resp.get("ok") and resp.get("result"):
            for update in reversed(resp["result"]):
                chat = None
                if "message" in update and "chat" in update["message"]:
                    chat = update["message"]["chat"]
                elif "channel_post" in update and "chat" in update["channel_post"]:
                    chat = update["channel_post"]["chat"]
                elif "my_chat_member" in update and "chat" in update["my_chat_member"]:
                    chat = update["my_chat_member"]["chat"]
                elif "callback_query" in update and "message" in update["callback_query"]:
                    chat = update["callback_query"]["message"].get("chat")
                if chat and "id" in chat:
                    cid = str(chat["id"])
                    set_active_chat_id(cid)
                    title = chat.get("title") or chat.get("username") or chat.get("first_name") or cid
                    return cid, title
    except Exception as e:
        add_log(f"Error querying Telegram getUpdates: {str(e)}")
    return None, None


def get_main_keyboard(is_running=None):
    if is_running is None:
        with automation_lock:
            is_running = automation_running
    toggle_btn = {"text": "⏸️ Pause"} if is_running else {"text": "▶️ Resume"}
    return {
        "keyboard": [
            [toggle_btn, {"text": "📊 Status"}],
            [{"text": "📋 Recent Logs"}, {"text": "📅 Daily Report"}],
            [{"text": "📡 Live Logs ON"}, {"text": "🔕 Live Logs OFF"}],
            [{"text": "🏓 Ping"}]
        ],
        "resize_keyboard": True,
        "is_persistent": False
    }


def register_bot_commands(token=None):
    token = token or DEFAULT_BOT_TOKEN
    if not token:
        return
    try:
        commands = [
            {"command": "status", "description": "📊 Real-time snatcher status & specs"},
            {"command": "pause", "description": "⏸️ Pause snatcher provisioning loop"},
            {"command": "resume", "description": "▶️ Resume snatcher provisioning loop"},
            {"command": "logs", "description": "📋 View latest 10 activity logs"},
            {"command": "daily", "description": "📅 Comprehensive 24h daily summary"},
            {"command": "livelog", "description": "📡 Stream live logs to chat (on/off)"},
            {"command": "ping", "description": "🏓 Check bot responsiveness"},
            {"command": "help", "description": "❓ View command guide & features"}
        ]
        requests.post(f"https://api.telegram.org/bot{token}/setMyCommands", json={"commands": commands}, timeout=10)
    except Exception as e:
        add_log(f"Telegram setMyCommands note: {e}")


def send_telegram_message(bot_token, chat_id, message, tz_name=None, reply_markup=None):
    if not bot_token or not chat_id:
        return False, "Missing bot token or chat ID"
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
        if reply_markup is not False:
            payload["reply_markup"] = reply_markup if reply_markup is not None else get_main_keyboard()
        response = requests.post(url, json=payload, timeout=10)
        data = response.json()
        if data.get("ok"):
            return True, "Message sent"
        else:
            err_desc = data.get("description", "Unknown Telegram error")
            if "can't parse entities" in err_desc.lower() or "tag" in err_desc.lower() or "bad request" in err_desc.lower():
                plain_text = re.sub(r'<[^>]+>', '', message)
                fb_payload = {"chat_id": chat_id, "text": plain_text}
                if reply_markup is not False:
                    fb_payload["reply_markup"] = reply_markup if reply_markup is not None else get_main_keyboard()
                fb_resp = requests.post(url, json=fb_payload, timeout=10).json()
                if fb_resp.get("ok"):
                    return True, "Message sent (plain text fallback)"
            add_log(f"Telegram sendMessage error: {err_desc}")
            return False, err_desc
    except Exception as e:
        add_log(f"Telegram sendMessage exception: {str(e)}")
        return False, str(e)


def get_oci_username(config, identity_client):
    try:
        user_ocid = config.get('user')
        if not user_ocid:
            add_log("Username detection skipped: no user OCID in config")
            return None
        add_log(f"Fetching user info from Identity API...")
        user = identity_client.get_user(user_id=user_ocid).data
        name = getattr(user, 'name', None)
        email = getattr(user, 'email', None)
        desc = getattr(user, 'description', None)
        if name and email:
            result = f"{name} ({email})"
        elif name:
            result = name
        elif email:
            result = email
        elif desc and desc != user_ocid:
            result = desc
        else:
            result = user_ocid
        add_log(f"Detected OCI user: {result}")
        return result
    except oci.exceptions.ServiceError as e:
        add_log(f"Identity API error (status {e.status}): {e.message}")
        return None
    except Exception as e:
        add_log(f"Error fetching user info: {str(e)}")
        return None


def run_automated_creation(config, account_config, compute_client, network_client, identity_client,
                           retry_delay=60, randomize_delay=True, random_min=60, random_max=90,
                           telegram_bot_token=None, telegram_chat_id=None, tz_name=None,
                           skip_start_alert=False):
    global automation_running
    set_user_tz(tz_name)
    oci_username = None
    target_region = config.get('region', 'unknown')
    target_name = account_config.get('display_name', 'AlwaysFree-Bot')
    try:
        oci_username = get_oci_username(config, identity_client)
        if oci_username:
            add_log(f"OCI username detected: {oci_username}")
    except Exception as e:
        add_log(f"Could not detect OCI username: {str(e)}")
    try:
        block_client = oci.core.BlockstorageClient(config)
        ok, err = check_free_tier_limits(config, account_config, compute_client, block_client, identity_client)
        if not ok:
            add_log(f"Free tier limit check failed: {err}")
            return
        add_log(f"Initializing infrastructure scan inside: {target_region}...")
        ads = identity_client.list_availability_domains(compartment_id=config['tenancy']).data
        ad_list = [ad.name for ad in ads] if ads else []
        add_log(f"Availability domains found: {len(ad_list)} — {', '.join(ad_list)}")
        ad_preference = account_config.get('ad_preference', '')
        if ad_preference and ad_preference in ad_list:
            ad_list.remove(ad_preference)
            ad_list.insert(0, ad_preference)
            add_log(f"Using preferred AD: {ad_preference}")
        elif ad_preference:
            add_log(f"Preferred AD '{ad_preference}' not found, using auto-rotation")
        subnet_id = account_config.get('subnet_id')
        if not subnet_id:
            vcns = network_client.list_vcns(compartment_id=config['tenancy']).data
            if not vcns:
                add_log("Error: No VCN found.")
                return
            subnets = network_client.list_subnets(compartment_id=config['tenancy'], vcn_id=vcns[0].id).data
            if not subnets:
                add_log("Error: No subnet found.")
                return
            subnet_id = subnets[0].id
            add_log("Auto-selected subnet: " + subnet_id[:20] + "...")
        else:
            add_log("Using selected subnet: " + subnet_id[:20] + "...")
        image_id = account_config.get('image_id')
        if not image_id:
            add_log("Error: No OS image selected.")
            return
        ssh_key = account_config.get('ssh_key', '').strip()
        if not ssh_key:
            add_log("Error: SSH public key is required.")
            return
        valid_prefixes = ('ssh-rsa', 'ssh-ed25519', 'ssh-dss', 'ecdsa-sha2-nistp256',
                          'ecdsa-sha2-nistp384', 'ecdsa-sha2-nistp521', 'sk-ssh-ed25519')
        if not any(ssh_key.startswith(p) for p in valid_prefixes):
            add_log("Error: SSH key does not appear to be a valid public key.")
            return
        boot_gb = int(account_config.get('boot_volume_gb', 50))
        if boot_gb < 50:
            add_log("Boot volume raised to minimum 50 GB.")
            boot_gb = 50
        add_log(f"Setup Verified -> Subnet: {subnet_id[:20]}... | Image: {image_id[:20]}... | Zone: {ad_list[0] if ad_list else 'N/A'}")
        shape = account_config.get('shape', '')
        is_flex = '.Flex' in shape

        # Enforce rate-limiting safeguards to prevent HTTP 429
        if random_min < 50:
            random_min = 60
        if random_max < random_min + 15:
            random_max = random_min + 30
        if retry_delay < 50:
            retry_delay = 60

        # Specs rotation setup for ARM Flex (alternating between 4 OCPU / 24 GB and 2 OCPU / 12 GB)
        try_both_specs = account_config.get('try_both_specs', True)
        specs_rotation = account_config.get('specs_rotation')
        if not specs_rotation and is_flex and try_both_specs:
            specs_rotation = [
                {"ocpus": 4, "memory": 24, "label": "4 OCPU / 24 GB"},
                {"ocpus": 2, "memory": 12, "label": "2 OCPU / 12 GB"}
            ]
        elif not specs_rotation and is_flex:
            ocpus_cfg = int(account_config.get('ocpus', 4))
            memory_cfg = int(account_config.get('memory', 24))
            specs_rotation = [
                {"ocpus": ocpus_cfg, "memory": memory_cfg, "label": f"{ocpus_cfg} OCPU / {memory_cfg} GB"}
            ]
        elif not specs_rotation:
            specs_rotation = []

        add_log(f"Debug -> Shape: {shape} | Boot: {boot_gb}GB | is_flex={is_flex} | Specs Rotation: {[s['label'] for s in specs_rotation] if specs_rotation else 'None'}")
        add_log(f"Debug -> Subnet details: assign_public_ip=True")

        shape_config = None
        if specs_rotation:
            first_spec = specs_rotation[0]
            shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=first_spec["ocpus"], memory_in_gbs=first_spec["memory"])
        elif is_flex:
            ocpus = int(account_config.get('ocpus', 2))
            memory = int(account_config.get('memory', 12))
            shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=ocpus, memory_in_gbs=memory)

        instance_details = oci.core.models.LaunchInstanceDetails(
            compartment_id=config['tenancy'],
            availability_domain=ad_list[0] if ad_list else '',
            shape=account_config['shape'],
            shape_config=shape_config,
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                image_id=image_id, boot_volume_size_in_gbs=boot_gb
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=subnet_id, assign_public_ip=True
            ),
            metadata={"ssh_authorized_keys": ssh_key},
            display_name=target_name
        )
        add_log(f"Launching provisioning loop for '{target_name}'...")
        attempts = 0
        success = False
        consecutive_429 = 0
        last_summary_time = time.time()
        start_time = time.time()

        with stats_lock:
            automation_stats["running"] = True
            automation_stats["start_time"] = start_time
            automation_stats["target_name"] = target_name
            automation_stats["shape"] = account_config.get('shape', 'VM.Standard.A1.Flex')
            automation_stats["boot_volume_gb"] = boot_gb
            automation_stats["region"] = target_region
            automation_stats["ad"] = ad_list[0] if ad_list else ''
            automation_stats["oci_username"] = oci_username
            automation_stats["last_status"] = "Starting launch attempts..."
            if specs_rotation and len(specs_rotation) > 1:
                automation_stats["specs_mode"] = "Dual Rotation (4 OCPU / 24 GB & 2 OCPU / 12 GB)"
                automation_stats["current_spec_label"] = specs_rotation[0]["label"]
                automation_stats["ocpus"] = specs_rotation[0]["ocpus"]
                automation_stats["memory_gb"] = specs_rotation[0]["memory"]
            elif specs_rotation:
                automation_stats["specs_mode"] = specs_rotation[0]["label"]
                automation_stats["current_spec_label"] = specs_rotation[0]["label"]
                automation_stats["ocpus"] = specs_rotation[0]["ocpus"]
                automation_stats["memory_gb"] = specs_rotation[0]["memory"]
            else:
                automation_stats["specs_mode"] = account_config.get('shape', 'VM.Standard.A1.Flex')
                automation_stats["current_spec_label"] = account_config.get('shape', 'VM.Standard.A1.Flex')
                automation_stats["ocpus"] = account_config.get('ocpus', 4)
                automation_stats["memory_gb"] = account_config.get('memory', 24)

        # Send initial start alert to Telegram
        if not skip_start_alert:
            init_chat = telegram_chat_id or get_active_chat_id()
            if telegram_bot_token and init_chat:
                specs_desc = "Dual Rotation (4 OCPU / 24 GB & 2 OCPU / 12 GB)" if (specs_rotation and len(specs_rotation) > 1) else f"{account_config.get('ocpus', 4)} OCPU, {account_config.get('memory', 24)} GB RAM"
                start_msg = (
                    "🚀 <b>Oracle A1 VM Snatcher Started!</b>\n\n"
                    f"• <b>Target:</b> {target_name}\n"
                    f"• <b>Shape:</b> {account_config.get('shape', 'VM.Standard.A1.Flex')}\n"
                    f"• <b>Specs:</b> {specs_desc}\n"
                    f"• <b>Region:</b> {target_region}\n"
                    f"• <b>Cadence:</b> 60–90s randomized (429 Rate-Limit Guard active)\n"
                    f"• <b>Time:</b> {format_user_time(tz_name=get_current_tz())}\n\n"
                    "<i>Monitoring capacity continuously 24/7. Immediate notification sent upon provisioning!</i>"
                )
                send_telegram_message(telegram_bot_token, init_chat, start_msg, get_current_tz(), reply_markup=get_main_keyboard(is_running=True))
        ad_index = 0
        import random as _random
        if len(ad_list) > 1:
            _random.shuffle(ad_list)
            add_log(f"AD order randomized for faster discovery: {', '.join(ad_list)}")
        while True:
            attempts += 1
            current_chat = telegram_chat_id or get_active_chat_id()
            now = time.time()
            now_date_str = datetime.datetime.now(get_user_tz(get_current_tz())).strftime('%Y-%m-%d')
            with stats_lock:
                automation_stats["total_attempts"] += 1
                if automation_stats["today_date"] != now_date_str:
                    automation_stats["today_date"] = now_date_str
                    automation_stats["attempts_today"] = 1
                else:
                    automation_stats["attempts_today"] += 1
                automation_stats["last_attempt_time"] = now
                if 'current_ad' in locals() and current_ad:
                    automation_stats["ad"] = current_ad

            # Determine spec for this attempt (alternating if dual mode)
            current_spec = None
            if specs_rotation:
                current_spec = specs_rotation[(attempts - 1) % len(specs_rotation)]
                instance_details.shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
                    ocpus=current_spec["ocpus"],
                    memory_in_gbs=current_spec["memory"]
                )
                spec_label = current_spec["label"]
            else:
                spec_label = f"{account_config.get('ocpus', 4)} OCPU / {account_config.get('memory', 24)} GB"

            with stats_lock:
                automation_stats["current_spec_label"] = spec_label
                if current_spec:
                    automation_stats["ocpus"] = current_spec["ocpus"]
                    automation_stats["memory_gb"] = current_spec["memory"]

            # Send periodic status update every 6 hours or every 50 attempts
            if telegram_bot_token and current_chat and ((now - last_summary_time >= 21600) or (attempts > 1 and attempts % 50 == 0)):
                last_summary_time = now
                elapsed_min = int((now - start_time) / 60)
                elapsed_hr = elapsed_min // 60
                elapsed_rem_min = elapsed_min % 60
                specs_desc = "Dual Rotation (4 OCPU / 24 GB & 2 OCPU / 12 GB)" if (specs_rotation and len(specs_rotation) > 1) else spec_label
                summary_msg = (
                    "📊 <b>OCI Snatcher Status Update</b>\n\n"
                    "• <b>Status:</b> Actively Retrying\n"
                    f"• <b>Attempts:</b> {attempts}\n"
                    f"• <b>Target Specs:</b> {specs_desc}\n"
                    f"• <b>Current Attempt:</b> {spec_label}\n"
                    f"• <b>Region:</b> {target_region} ({current_ad if 'current_ad' in locals() else 'AD-1'})\n"
                    f"• <b>Uptime:</b> {elapsed_hr}h {elapsed_rem_min}m\n"
                    f"• <b>Time:</b> {format_user_time(tz_name=get_current_tz())}\n\n"
                    "<i>Capacity currently busy in your region. Monitoring 24/7 for available slots.</i>"
                )
                send_telegram_message(telegram_bot_token, current_chat, summary_msg, get_current_tz())
            if stop_event.is_set():
                add_log("Provisioning loop stopped by user.")
                break
            current_ad = ad_list[ad_index % len(ad_list)] if ad_list else ''
            if len(ad_list) > 1:
                add_log(f"Attempt {attempts}: trying AD '{current_ad}'...")
            instance_details.availability_domain = current_ad
            is_429 = False
            try:
                add_log(f"Attempt {attempts} [{spec_label}]: sending instance launch request...")
                response = compute_client.launch_instance(instance_details)
                consecutive_429 = 0
                instance_id = response.data.id
                add_log(f"SUCCESS! Instance created: {instance_id[:20]}...")
                add_log("Fetching instance public IP...")
                public_ip, ip_err = get_instance_public_ip(config, compute_client, network_client, instance_id)
                if public_ip:
                    add_log(f"Public IP: {public_ip}")
                elif ip_err:
                    add_log(f"Could not get public IP: {ip_err}")
                success = True
                with stats_lock:
                    automation_stats["success"] = True
                    automation_stats["instance_id"] = instance_id
                    automation_stats["public_ip"] = public_ip
                    automation_stats["last_status"] = f"Successfully Provisioned ({spec_label})!"

                target_tg_chat = telegram_chat_id or get_active_chat_id()
                if telegram_bot_token and target_tg_chat:
                    instance_name = account_config.get('display_name', 'AlwaysFree-Bot')
                    shape = account_config.get('shape', 'Unknown')
                    region = config.get('region', 'unknown')
                    user_time = format_user_time(tz_name=get_current_tz())
                    user_line = f"<b>User:</b> {oci_username}\n" if oci_username else ""
                    ip_line = f"<b>Public IP:</b> {public_ip}\n" if public_ip else ""
                    tg_msg = (
                        f"&#9989; <b>OCI Provisioner Success!</b>\n\n"
                        f"<b>Instance:</b> {instance_name}\n"
                        f"<b>Shape:</b> {shape} ({spec_label})\n"
                        f"<b>Region:</b> {region}\n"
                        f"{ip_line}"
                        f"{user_line}"
                        f"<b>Time:</b> {user_time}\n"
                        f"<b>Status:</b> Running\n\n"
                        f"Your Always Free instance ({spec_label}) has been successfully provisioned!"
                    )
                    tg_ok, tg_err = send_telegram_message(telegram_bot_token, target_tg_chat, tg_msg, get_current_tz())
                    if tg_ok:
                        add_log("Telegram success alert sent.")
                    else:
                        add_log(f"Telegram alert failed: {tg_err}")
                break
            except oci.exceptions.ServiceError as e:
                msg = str(e)
                code = getattr(e, 'code', 'N/A')
                status = getattr(e, 'status', 'N/A')
                add_log(f"Debug -> ServiceError code={code}, status={status}, msg={e.message[:120]}")
                if "TooManyRequests" in msg or status == 429:
                    is_429 = True
                    consecutive_429 += 1
                    with stats_lock:
                        automation_stats["rate_limit_count"] += 1
                        automation_stats["last_status"] = "Too Many Requests (429 Rate Limited)"
                    user_info = f" [user: {oci_username}]" if oci_username else ""
                    add_log(f"Rate limited (429) in '{target_region}'.{user_info} (streak: {consecutive_429}) Backing off...")
                elif "Out of capacity" in msg or status in (500, 503, 504):
                    consecutive_429 = 0
                    with stats_lock:
                        automation_stats["capacity_busy_count"] += 1
                        automation_stats["last_status"] = f"Out of host capacity (500) for {spec_label}"
                    user_info = f" [user: {oci_username}]" if oci_username else ""
                    add_log(f"Capacity busy in '{target_region}' AD '{current_ad}' for [{spec_label}].{user_info} Retrying...")
                    if len(ad_list) > 1:
                        ad_index += 1
                        next_ad = ad_list[ad_index % len(ad_list)]
                        add_log(f"Switching to next AD: '{next_ad}'")
                elif "NotAuthorizedOrNotFound" in msg or "Authorization failed" in msg or status == 404:
                    with stats_lock:
                        automation_stats["error_count"] += 1
                        automation_stats["last_status"] = "Auth/NotFound error"
                    add_log(f"Auth/NotFound error — possible causes:")
                    add_log(f"  1. Image {image_id[:25]}... not found in AD {current_ad}")
                    add_log(f"  2. Shape {account_config['shape']} not available in this AD")
                    add_log(f"  3. Subnet {subnet_id[:25]}... missing permissions")
                    add_log(f"  4. Check OCI Console > Instances > Create — test manually")
                    if len(ad_list) > 1:
                        ad_index += 1
                        add_log(f"Trying next AD...")
                        continue
                    break
                else:
                    with stats_lock:
                        automation_stats["error_count"] += 1
                        automation_stats["last_status"] = f"OCI Error: {e.message[:50]}"
                    add_log(f"OCI API error: {e.message}")
                    if len(ad_list) > 1:
                        ad_index += 1
                        add_log(f"Trying next AD...")
                        continue
                    break
            except (ConnectionError, OSError) as e:
                consecutive_429 = 0
                user_info = f" [user: {oci_username}]" if oci_username else ""
                add_log(f"Connection issue in '{target_region}': {type(e).__name__}.{user_info} Retrying...")
            except Exception as e:
                consecutive_429 = 0
                msg = str(e)
                if "Remote end closed connection" in msg or "Connection aborted" in msg or "timeout" in msg.lower():
                    user_info = f" [user: {oci_username}]" if oci_username else ""
                    add_log(f"Network hiccup in '{target_region}': connection dropped.{user_info} Retrying...")
                else:
                    add_log(f"Automation engine failure: {msg}")
                    break
            actual_delay = retry_delay
            if randomize_delay:
                actual_delay = random.randint(random_min, random_max)
            if is_429:
                backoff_base = 120 + min((consecutive_429 - 1) * 30, 180)
                actual_delay = random.randint(backoff_base, backoff_base + 45)
                add_log(f"Rate limit guard: backing off for {actual_delay}s to allow API token bucket to reset...")
            else:
                add_log(f"Dynamic retry: waiting {actual_delay}s (randomized {random_min}-{random_max}s)")
            if stop_event.wait(actual_delay):
                add_log("Provisioning loop stopped while waiting.")
                break
        if not success:
            was_stopped = stop_event.is_set()
            global loop_stop_reason
            stopped_by_telegram = was_stopped and (loop_stop_reason == "telegram")
            add_log("Provisioning loop stopped by user." if was_stopped else "Provisioning loop ended without success.")
            target_tg_chat = telegram_chat_id or get_active_chat_id()
            if telegram_bot_token and target_tg_chat and not stopped_by_telegram:
                user_line = f"<b>User:</b> {oci_username}\n" if oci_username else ""
                user_time = format_user_time(tz_name=get_current_tz())
                if was_stopped:
                    tg_msg = (
                        f"⏸️ <b>OCI Provisioner Paused</b>\n\n"
                        f"{user_line}"
                        f"Provisioning loop paused via Web Dashboard after {attempts} attempts.\n"
                        f"<b>Region:</b> {config.get('region', 'unknown')}\n"
                        f"<b>Time:</b> {user_time}\n\n"
                        "Tap <b>▶️ Resume</b> below or send <code>/resume</code> anytime to continue."
                    )
                else:
                    tg_msg = (
                        f"&#10060; <b>OCI Provisioner Stopped</b>\n\n"
                        f"{user_line}"
                        f"Loop stopped after {attempts} attempts without success.\n"
                        f"<b>Region:</b> {config.get('region', 'unknown')}\n"
                        f"<b>Time:</b> {user_time}\n\n"
                        "Tap <b>▶️ Resume</b> below or send <code>/resume</code> to restart."
                    )
                send_telegram_message(telegram_bot_token, target_tg_chat, tg_msg, get_current_tz(), reply_markup=get_main_keyboard(is_running=False))
    except Exception as e:
        msg = str(e)
        if "Remote end closed connection" in msg or "Connection aborted" in msg:
            add_log(f"Network connection lost. Loop ended.")
        else:
            add_log(f"Automation engine failure: {msg}")
        if telegram_bot_token and telegram_chat_id:
            user_line = f"<b>User:</b> {oci_username}\n" if oci_username else ""
            user_time = format_user_time(tz_name=get_current_tz())
            tg_msg = (
                f"&#10060; <b>OCI Provisioner Error</b>\n\n"
                f"{user_line}"
                f"Automation engine failure:\n{msg[:200]}\n"
                f"<b>Time:</b> {user_time}"
            )
            send_telegram_message(telegram_bot_token, target_tg_chat, tg_msg, get_current_tz())
    finally:
        with automation_lock:
            automation_running = False
            automation_shape = None
        with stats_lock:
            automation_stats["running"] = False


@app.route('/api/free-tier-status', methods=['POST'])
@require_auth
def free_tier_status():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    config = build_config(data)
    try:
        oci.config.validate_config(config)
        compute_client = oci.core.ComputeClient(config)
        block_client = oci.core.BlockstorageClient(config)
        identity_client = oci.identity.IdentityClient(config)
        usage = get_free_tier_usage(config, compute_client, block_client, identity_client)
        return jsonify({'success': True, 'usage': usage})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/status', methods=['GET'])
@require_auth
def get_status():
    with automation_lock:
        return jsonify({
            'success': True,
            'running': automation_running,
            'shape': automation_shape,
            'default_bot_token': DEFAULT_BOT_TOKEN,
            'default_chat_id': DEFAULT_CHAT_ID
        })


def start_snatcher_loop(data, source="api"):
    global automation_running, automation_shape, tg_live_enabled, tg_live_bot_token, tg_live_chat_id, loop_stop_reason
    if not data or not data.get('user') or not data.get('private_key'):
        return False, "No valid configuration found. Please configure the snatcher from the web UI first."

    config = build_config(data)
    try:
        oci.config.validate_config(config)
    except Exception as e:
        return False, f"Invalid OCI config: {e}"

    requested_shape = data.get('shape', 'VM.Standard.A1.Flex')
    bot_token = (data.get('telegram_bot_token') or '').strip() or DEFAULT_BOT_TOKEN
    chat_id = (data.get('telegram_chat_id') or '').strip() or DEFAULT_CHAT_ID

    if bot_token and (not chat_id or str(chat_id).lower() == 'auto'):
        detected_id, _ = resolve_telegram_chat(bot_token)
        if detected_id:
            chat_id = detected_id
            set_active_chat_id(chat_id)
    elif chat_id:
        set_active_chat_id(chat_id)

    enable_live = data.get('telegram_live_log', False)
    with tg_live_lock:
        tg_live_enabled = bool(enable_live and bot_token and chat_id)
        tg_live_bot_token = bot_token if enable_live else None
        tg_live_chat_id = chat_id if enable_live else None
        tg_live_last_sent = 0

    with automation_lock:
        if automation_running:
            if automation_shape and automation_shape != requested_shape:
                return False, f"A provisioning loop is already running for shape '{automation_shape}'. Stop it first before starting '{requested_shape}'."
            return False, "A provisioning loop is already running."
        automation_running = True
        automation_shape = requested_shape
        stop_event.clear()
        loop_stop_reason = None

    try:
        compute_client = oci.core.ComputeClient(config)
        network_client = oci.core.VirtualNetworkClient(config)
        identity_client = oci.identity.IdentityClient(config)
        retry_delay = int(data.get('retry_delay', 60))
        if retry_delay < 50:
            retry_delay = 60
        randomize_delay = data.get('randomize_delay', True)
        random_min = max(int(data.get('random_min', 60)), 50)
        random_max = max(int(data.get('random_max', 90)), random_min + 15)
        tz = data.get('timezone', 'Asia/Jakarta')
        set_user_tz(tz)

        skip_start_alert = (source == "telegram")

        thread = threading.Thread(
            target=run_automated_creation,
            args=(config, data, compute_client, network_client, identity_client,
                  retry_delay, randomize_delay, random_min, random_max,
                  bot_token, chat_id, tz, skip_start_alert),
            daemon=True
        )
        thread.start()

        save_active_config(data)

        return True, "Provisioning loop started." + (" Live Telegram logging enabled." if tg_live_enabled else "")
    except Exception as e:
        with automation_lock:
            automation_running = False
            automation_shape = None
        return False, str(e)


def stop_snatcher_loop(source="web"):
    global tg_live_enabled, loop_stop_reason
    with automation_lock:
        if not automation_running:
            return False, "Provisioning loop is not currently running."
        loop_stop_reason = source
    stop_event.set()
    with tg_live_lock:
        tg_live_enabled = False
    return True, "Stop signal sent."


@app.route('/api/auto-launch-loop', methods=['POST'])
@require_auth
def auto_launch():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    ok, msg = start_snatcher_loop(data, source="api")
    if not ok:
        return jsonify({'success': False, 'error': msg})
    return jsonify({'success': True, 'message': msg})


@app.route('/api/stop-loop', methods=['POST'])
@require_auth
def stop_loop():
    ok, msg = stop_snatcher_loop(source="web")
    return jsonify({'success': True, 'message': msg})


@app.route('/api/logs', methods=['GET'])
@require_auth
def fetch_live_logs():
    offset = int(request.args.get('offset', 0))
    with logs_lock:
        log_list = list(global_logs)
        total = len(log_list)
        if offset > total:
            offset = 0
        batch = log_list[offset:]
    return jsonify({'logs': batch, 'next_offset': total})


@app.route('/api/detect-telegram-chat', methods=['POST'])
@require_auth
def detect_telegram_chat():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    token = data.get('bot_token', '').strip() or DEFAULT_BOT_TOKEN
    chat_id, chat_title = resolve_telegram_chat(token)
    if chat_id:
        user_time = format_user_time(tz_name=get_current_tz())
        welcome_msg = (
            "🚀 <b>Oracle A1 VM Snatcher Connected!</b>\n\n"
            f"<b>Chat:</b> {chat_title} (<code>{chat_id}</code>)\n"
            f"<b>Time:</b> {user_time}\n\n"
            "You will receive notifications here when your Always Free ARM instance is successfully provisioned."
        )
        send_telegram_message(token, chat_id, welcome_msg, get_current_tz())
        return jsonify({
            'success': True,
            'chat_id': chat_id,
            'chat_title': chat_title,
            'message': f'Connected to chat {chat_id} ({chat_title})'
        })
    else:
        return jsonify({
            'success': False,
            'error': 'No updates found in Telegram yet. Please open @cuanglabbot on Telegram and send /start or any message, then try again.'
        })



@app.route('/api/test-telegram', methods=['POST'])
@require_auth
def test_telegram():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    bot_token = data.get('bot_token', '').strip() or DEFAULT_BOT_TOKEN
    chat_id = data.get('chat_id', '').strip()
    if not chat_id or chat_id.lower() == 'auto':
        chat_id, title = resolve_telegram_chat(bot_token)
        if not chat_id:
            return jsonify({'success': False, 'error': 'Chat ID not set and none found in Telegram updates. Send /start to @cuanglabbot on Telegram first.'})
    user_time = format_user_time(tz_name=get_current_tz())
    ok, err = send_telegram_message(
        bot_token, chat_id,
        f"&#9989; <b>OCI Instance loop Connected</b>\n\n"
        f"Your Telegram alerts are now active.\n"
        f"<b>Time:</b> {user_time}\n\n"
        f"You will receive notifications when provisioning succeeds or fails.",
        get_current_tz()
    )
    if ok:
        return jsonify({'success': True, 'message': 'Test message sent successfully'})
    return jsonify({'success': False, 'error': err})


@app.route('/api/send-telegram', methods=['POST'])
@require_auth
def send_telegram():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    ok, err = send_telegram_message(
        data.get('bot_token'), data.get('chat_id'), data.get('message', ''), get_current_tz()
    )
    return jsonify({'success': ok, 'error': err})


@app.route('/api/list-instances', methods=['POST'])
@require_auth
def api_list_instances():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    config = build_config(data)
    try:
        oci.config.validate_config(config)
        compute_client = oci.core.ComputeClient(config)
        identity_client = oci.identity.IdentityClient(config)
        instances = list_all_instances(config, compute_client, identity_client)
        return jsonify({'success': True, 'instances': instances})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/delete-instance', methods=['POST'])
@require_auth
def api_delete_instance():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    config = build_config(data)
    instance_id = data.get('instance_id')
    if not instance_id:
        return jsonify({'success': False, 'error': 'instance_id required'})
    try:
        oci.config.validate_config(config)
        compute_client = oci.core.ComputeClient(config)
        try:
            inst = compute_client.get_instance(instance_id=instance_id).data
            name = inst.display_name
        except:
            name = instance_id[:20]
        ok, err = terminate_instance(compute_client, instance_id)
        if ok:
            add_log(f"Instance '{name}' ({instance_id[:20]}...) termination initiated.")
            return jsonify({'success': True, 'message': f"Instance '{name}' termination initiated"})
        else:
            return jsonify({'success': False, 'error': err})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})




@app.route('/api/reboot-instance', methods=['POST'])
@require_auth
def api_reboot_instance():
    """Reboot a single instance by ID."""
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    config = build_config(data)
    instance_id = data.get('instance_id')

    if not instance_id:
        return jsonify({'success': False, 'error': 'instance_id required'})

    try:
        oci.config.validate_config(config)
        compute_client = oci.core.ComputeClient(config)

        # Get instance name before rebooting for logging
        try:
            inst = compute_client.get_instance(instance_id=instance_id).data
            name = inst.display_name
        except:
            name = instance_id[:20]

        compute_client.instance_action(instance_id=instance_id, action='RESET')
        add_log(f"Instance '{name}' ({instance_id[:20]}...) reboot initiated.")
        return jsonify({'success': True, 'message': f"Instance '{name}' reboot initiated"})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/delete-all-instances', methods=['POST'])
@require_auth
def api_delete_all_instances():
    data = request.json or {}
    set_user_tz(data.get('timezone'))
    config = build_config(data)
    try:
        oci.config.validate_config(config)
        compute_client = oci.core.ComputeClient(config)
        identity_client = oci.identity.IdentityClient(config)
        instances = list_all_instances(config, compute_client, identity_client)
        if not instances:
            return jsonify({'success': True, 'message': 'No instances to delete', 'deleted': 0})
        deleted = 0
        failed = []
        for inst in instances:
            ok, err = terminate_instance(compute_client, inst['id'])
            if ok:
                add_log(f"Terminating '{inst['name']}' ({inst['id'][:20]}...)")
                deleted += 1
            else:
                failed.append({'name': inst['name'], 'error': err})
        return jsonify({'success': True, 'message': f"Initiated termination for {deleted} instance(s)", 'deleted': deleted, 'failed': failed})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)


# ---- Auto-Resume Saved Configuration on Container Start ----
def auto_resume_saved_config():
    cfg = get_saved_config()
    if cfg:
        add_log("Auto-resuming saved snatcher configuration...")
        ok, msg = start_snatcher_loop(cfg, source="auto_resume")
        if ok:
            add_log("Snatcher provisioning loop auto-resumed successfully.")
        else:
            add_log(f"Auto-resume notice: {msg}")


# ---- Telegram Bot Action Handlers ----
def handle_telegram_pause(token, chat_id):
    with automation_lock:
        is_running = automation_running
    if not is_running:
        reply = (
            "⏸️ <b>Snatcher Is Already Paused</b>\n\n"
            "The provisioning loop is not currently active.\n\n"
            "Tap <b>▶️ Resume</b> below or send <code>/resume</code> anytime to start snatching."
        )
        send_telegram_message(token, chat_id, reply, reply_markup=get_main_keyboard(is_running=False))
        return

    stop_snatcher_loop(source="telegram")
    with stats_lock:
        st = automation_stats.copy()
    tz = get_user_tz(get_current_tz())
    now_dt = datetime.datetime.now(tz)
    target = st.get('target_name', 'AlwaysFree-ARM')
    shape = st.get('shape', 'VM.Standard.A1.Flex')
    attempts_total = st.get('total_attempts', 0)
    last_st = st.get('last_status', 'Paused by user')
    region = st.get('region', 'ap-singapore-1')
    reply = (
        "⏸️ <b>Oracle A1 VM Snatcher Paused</b>\n\n"
        "The provisioning loop has been paused. No further launch requests will be sent to Oracle Cloud.\n\n"
        f"• <b>Target:</b> {html.escape(str(target))}\n"
        f"• <b>Shape:</b> <code>{html.escape(str(shape))}</code>\n"
        f"• <b>Total Attempts:</b> {attempts_total:,} requests\n"
        f"• <b>Last Status:</b> {html.escape(str(last_st))}\n"
        f"• <b>Region:</b> {html.escape(str(region))}\n"
        f"• <b>Time:</b> {now_dt.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "Tap <b>▶️ Resume</b> below or send <code>/resume</code> anytime to continue snatching."
    )
    send_telegram_message(token, chat_id, reply, reply_markup=get_main_keyboard(is_running=False))


def handle_telegram_resume(token, chat_id):
    if stop_event.is_set():
        for _ in range(30):
            with automation_lock:
                if not automation_running:
                    break
            time.sleep(0.1)

    with automation_lock:
        is_running = automation_running
    if is_running:
        reply = (
            "🟢 <b>Snatcher Is Already Running</b>\n\n"
            "The provisioning loop is actively monitoring and querying Oracle Cloud for freed capacity 24/7.\n\n"
            "• Send <code>/status</code> to check real-time progress\n"
            "• Tap <b>⏸️ Pause</b> below to pause the snatcher"
        )
        send_telegram_message(token, chat_id, reply, reply_markup=get_main_keyboard(is_running=True))
        return

    saved_cfg = get_saved_config()
    if not saved_cfg:
        reply = (
            "⚠️ <b>No Active Configuration Found</b>\n\n"
            "Unable to resume: no saved OCI credentials or settings were found.\n\n"
            "Please open the Web Dashboard, configure your tenancy details, and start the loop at least once so your settings are saved."
        )
        send_telegram_message(token, chat_id, reply, reply_markup=get_main_keyboard(is_running=False))
        return

    # Update telegram chat ID and token in saved config to this active chat
    saved_cfg['telegram_chat_id'] = chat_id
    if token:
        saved_cfg['telegram_bot_token'] = token

    ok, err_or_msg = start_snatcher_loop(saved_cfg, source="telegram")
    if not ok:
        reply = (
            "⚠️ <b>Failed to Resume Snatcher</b>\n\n"
            f"<b>Error:</b> <code>{html.escape(str(err_or_msg))}</code>\n\n"
            "Please check your credentials in the Web Dashboard."
        )
        send_telegram_message(token, chat_id, reply, reply_markup=get_main_keyboard(is_running=False))
        return

    tz = get_user_tz(saved_cfg.get('timezone'))
    now_dt = datetime.datetime.now(tz)
    target = saved_cfg.get('display_name', 'AlwaysFree-ARM')
    shape = saved_cfg.get('shape', 'VM.Standard.A1.Flex')
    specs_rot = saved_cfg.get('specs_rotation')
    if specs_rot and len(specs_rot) > 1:
        specs_desc = "Dual Rotation (4 OCPU / 24 GB & 2 OCPU / 12 GB)"
    elif specs_rot:
        specs_desc = specs_rot[0].get('label', '4 OCPU / 24 GB')
    else:
        specs_desc = f"{saved_cfg.get('ocpus', 4)} OCPU / {saved_cfg.get('memory', 24)} GB RAM"
    region = saved_cfg.get('region', 'ap-singapore-1')
    reply = (
        "▶️ <b>Oracle A1 VM Snatcher Resumed!</b>\n\n"
        "The provisioning loop has been resumed successfully.\n\n"
        f"• <b>Target:</b> {html.escape(str(target))}\n"
        f"• <b>Shape:</b> <code>{html.escape(str(shape))}</code>\n"
        f"• <b>Specs:</b> {html.escape(str(specs_desc))}\n"
        f"• <b>Region:</b> {html.escape(str(region))}\n"
        "• <b>Cadence:</b> 60–90s randomized (429 Rate-Limit Guard active)\n"
        f"• <b>Time:</b> {now_dt.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "<i>Monitoring capacity continuously 24/7. Immediate notification sent upon provisioning!</i>"
    )
    send_telegram_message(token, chat_id, reply, reply_markup=get_main_keyboard(is_running=True))


# ---- Dedicated Periodic Updater (Daily Updates & Heartbeats) ----
def start_periodic_updater():
    def _updater():
        add_log("Periodic daily updater thread initialized.")
        last_heartbeat = time.time()
        while True:
            try:
                time.sleep(30)
                now = time.time()
                tz = get_user_tz(get_current_tz())
                now_dt = datetime.datetime.now(tz)
                current_date_str = now_dt.strftime('%Y-%m-%d')
                current_hour = now_dt.hour
                current_min = now_dt.minute

                # Daily updates twice a day: Morning (09:00 WIB) and Evening (21:00 WIB)
                slot_id = None
                if current_hour == 9 and current_min < 15:
                    slot_id = f"{current_date_str}-morning"
                elif current_hour == 21 and current_min < 15:
                    slot_id = f"{current_date_str}-evening"

                chat_id = get_active_chat_id()
                token = DEFAULT_BOT_TOKEN

                if slot_id and token and chat_id:
                    with stats_lock:
                        already_sent = slot_id in automation_stats["daily_reports_sent"]
                    if not already_sent:
                        with stats_lock:
                            automation_stats["daily_reports_sent"].add(slot_id)
                        report_msg = generate_daily_report_text()
                        ok, err = send_telegram_message(token, chat_id, report_msg)
                        if ok:
                            add_log(f"Daily update ({slot_id}) sent to Telegram chat {chat_id}")
                        else:
                            add_log(f"Failed to send daily update ({slot_id}): {err}")
                        last_heartbeat = now

                # Heartbeat check every 6 hours if running
                if now - last_heartbeat >= 21600 and token and chat_id:
                    with automation_lock:
                        is_running = automation_running
                    if is_running:
                        last_heartbeat = now
                        heartbeat_msg = generate_status_text()
                        send_telegram_message(token, chat_id, heartbeat_msg)
                        add_log(f"6-hour status heartbeat sent to Telegram chat {chat_id}")
            except Exception as e:
                time.sleep(10)

    t = threading.Thread(target=_updater, daemon=True, name="PeriodicDailyUpdater")
    t.start()


# ---- Interactive Telegram Bot Listener ----
def start_telegram_bot_listener():
    def _listener():
        add_log("Telegram interactive bot listener started.")
        last_offset = 0
        # Initialize last_offset with latest updates so we don't replay ancient messages
        try:
            token = DEFAULT_BOT_TOKEN
            if token:
                init_res = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                                        params={"offset": -1, "timeout": 5}, timeout=10).json()
                if init_res.get("ok") and init_res.get("result"):
                    last_offset = init_res["result"][-1]["update_id"] + 1
                    add_log(f"Telegram listener offset synchronized to {last_offset}")
        except Exception as e:
            add_log(f"Telegram offset sync note: {e}")

        while True:
            try:
                token = DEFAULT_BOT_TOKEN
                if not token:
                    time.sleep(10)
                    continue
                url = f"https://api.telegram.org/bot{token}/getUpdates"
                params = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
                if last_offset:
                    params["offset"] = last_offset
                
                try:
                    resp = requests.get(url, params=params, timeout=35).json()
                except (requests.exceptions.ReadTimeout, requests.exceptions.Timeout):
                    continue
                except Exception:
                    time.sleep(3)
                    continue

                if resp.get("ok") and resp.get("result"):
                    for update in resp["result"]:
                        update_id = update["update_id"]
                        if update_id >= last_offset:
                            last_offset = update_id + 1

                        # Handle Inline Keyboard callbacks
                        cb = update.get("callback_query")
                        if cb:
                            cb_id = cb.get("id")
                            cb_data = (cb.get("data") or "").strip().lower()
                            cb_msg = cb.get("message", {})
                            chat = cb_msg.get("chat", {})
                            chat_id = str(chat.get("id"))
                            set_active_chat_id(chat_id)
                            try:
                                requests.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                                              json={"callback_query_id": cb_id}, timeout=5)
                            except Exception:
                                pass
                            if cb_data in ("pause", "tg_pause"):
                                handle_telegram_pause(token, chat_id)
                            elif cb_data in ("resume", "tg_resume"):
                                handle_telegram_resume(token, chat_id)
                            elif cb_data in ("status", "tg_status"):
                                send_telegram_message(token, chat_id, generate_status_text())
                            elif cb_data in ("logs", "tg_logs"):
                                send_telegram_message(token, chat_id, generate_logs_text())
                            elif cb_data in ("daily", "tg_daily"):
                                send_telegram_message(token, chat_id, generate_daily_report_text())
                            continue

                        msg = update.get("message")
                        if not msg:
                            continue
                        chat = msg.get("chat", {})
                        chat_id = str(chat.get("id"))
                        set_active_chat_id(chat_id)
                        text = (msg.get("text") or "").strip()
                        text_lower = text.lower()
                        user_name = chat.get("first_name") or "William"
                        add_log(f"Telegram msg received from {user_name} ({chat_id}): '{text}'")

                        # Intent matching with robust button and emoji support
                        is_live_on = ("live" in text_lower or "stream" in text_lower) and "on" in text_lower
                        is_live_off = ("live" in text_lower or "stream" in text_lower) and ("off" in text_lower or "stop" in text_lower)
                        is_daily = "daily" in text_lower or "report" in text_lower or "summary" in text_lower
                        is_logs = ("log" in text_lower) and not is_live_on and not is_live_off
                        is_status = "status" in text_lower or "stat" in text_lower or "state" in text_lower or "check" in text_lower or "how" in text_lower
                        is_ping = "ping" in text_lower

                        # Pause / Resume matching
                        is_pause = (
                            not is_live_off and (
                                "pause" in text_lower or
                                text_lower.startswith("/pause") or
                                text_lower == "stop" or
                                text_lower.startswith("/stop") or
                                "⏸️" in text or
                                text_lower == "stop snatcher"
                            )
                        )
                        is_resume = (
                            "resume" in text_lower or
                            text_lower.startswith("/resume") or
                            "unpause" in text_lower or
                            "▶️" in text or
                            text_lower == "resume snatcher" or
                            text_lower == "start snatcher"
                        )

                        if "pause/resume" in text_lower or "pause / resume" in text_lower or text_lower == "toggle":
                            with automation_lock:
                                is_running = automation_running
                            if is_running:
                                is_pause = True
                                is_resume = False
                            else:
                                is_resume = True
                                is_pause = False

                        is_help = not is_pause and not is_resume and (
                            "help" in text_lower or text_lower.startswith("/start") or text_lower == "start" or "menu" in text_lower
                        )

                        # 1. Pause Snatcher
                        if is_pause:
                            handle_telegram_pause(token, chat_id)

                        # 2. Resume Snatcher
                        elif is_resume:
                            handle_telegram_resume(token, chat_id)

                        # 3. Live Log Toggle Commands
                        elif is_live_on:
                            global tg_live_enabled, tg_live_bot_token, tg_live_chat_id
                            with tg_live_lock:
                                tg_live_enabled = True
                                tg_live_bot_token = token
                                tg_live_chat_id = chat_id
                            reply = (
                                "📡 <b>Live Telegram Logging: ENABLED</b>\n\n"
                                "You will now receive real-time updates as each provisioning attempt occurs.\n\n"
                                "Tap <b>🔕 Live Logs OFF</b> below anytime to stop streaming."
                            )
                            send_telegram_message(token, chat_id, reply)

                        elif is_live_off:
                            with tg_live_lock:
                                tg_live_enabled = False
                            reply = (
                                "🔕 <b>Live Telegram Logging: DISABLED</b>\n\n"
                                "Live logs paused. You will still receive daily updates and the instant success alert when the VM is created.\n\n"
                                "Tap <b>📋 Recent Logs</b> anytime to view recent activity."
                            )
                            send_telegram_message(token, chat_id, reply)

                        # 4. Daily Report / Summary
                        elif is_daily:
                            reply = generate_daily_report_text()
                            send_telegram_message(token, chat_id, reply)

                        # 5. Recent Logs
                        elif is_logs:
                            num = 10
                            parts = text.split()
                            if len(parts) > 1 and parts[-1].isdigit():
                                num = max(3, min(int(parts[-1]), 30))
                            reply = generate_logs_text(num_lines=num)
                            send_telegram_message(token, chat_id, reply)

                        # 6. Status Check
                        elif is_status:
                            reply = generate_status_text()
                            send_telegram_message(token, chat_id, reply)

                        # 7. Ping
                        elif is_ping:
                            send_telegram_message(token, chat_id, "🏓 <b>Pong!</b> Snatcher service is active, responsive, and monitoring 24/7.")

                        # 8. Start / Help / Menu
                        elif is_help:
                            reply = (
                                f"👋 <b>Hello {user_name}!</b>\n\n"
                                "Welcome to <b>Oracle A1 VM Snatcher</b> bot.\n"
                                "Your Always Free Ampere A1 snatcher is actively alternating between <b>4 OCPU / 24 GB</b> and <b>2 OCPU / 12 GB</b> with 429 rate-limit protection.\n\n"
                                "<b>🔘 Quick Menu Options:</b>\n"
                                "Use the menu buttons below for 1-tap commands:\n\n"
                                "• <b>⏸️ Pause / ▶️ Resume</b> - Pause or resume the snatcher loop\n"
                                "• <b>📊 Status</b> - Real-time snatcher state & specs\n"
                                "• <b>📋 Recent Logs</b> - View latest 10 attempt logs\n"
                                "• <b>📅 Daily Report</b> - Comprehensive 24h summary report\n"
                                "• <b>📡 Live Logs ON</b> - Stream attempts live to this chat\n"
                                "• <b>🔕 Live Logs OFF</b> - Pause live streaming\n"
                                "• <b>🏓 Ping</b> - Test bot responsiveness\n\n"
                                "☀️ <b>Daily Updates:</b> Automatically sent twice daily (09:00 & 21:00 WIB).\n"
                                "🚀 <b>VM Capture Alert:</b> Sent immediately with public IP once created!"
                            )
                            send_telegram_message(token, chat_id, reply)

                        # 9. Fallback for ANY other message
                        else:
                            with automation_lock:
                                is_running = automation_running
                            status_badge = "🟢 <b>Actively Snatching (24/7)</b>" if is_running else "⏸️ <b>Idle / Paused</b>"
                            with logs_lock:
                                recent = list(global_logs)[-3:]
                            logs_clean = "\n".join(f"• <code>{html.escape(l)}</code>" for l in recent) if recent else "<i>No logs yet</i>"
                            reply = (
                                f"🤖 <b>Oracle A1 VM Snatcher:</b> {status_badge}\n\n"
                                f"<b>Latest Activity:</b>\n{logs_clean}\n\n"
                                "Tap any button on the menu below to control or check status:"
                            )
                            send_telegram_message(token, chat_id, reply)
            except Exception as e:
                add_log(f"Telegram listener error: {str(e)}")
                time.sleep(3)

    t = threading.Thread(target=_listener, daemon=True, name="TelegramBotListener")
    t.start()


# Auto-initialize services once per process
_services_started = False
_services_lock = threading.Lock()

def init_background_services():
    global _services_started
    with _services_lock:
        if _services_started:
            return
        _services_started = True
    register_bot_commands()
    auto_resume_saved_config()
    start_periodic_updater()
    start_telegram_bot_listener()

init_background_services()
