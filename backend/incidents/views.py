from django.shortcuts import render, redirect
from django.conf import settings
from datetime import datetime, timedelta
from django.http import HttpResponse, HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.cache import never_cache
from django.contrib.auth.decorators import login_required
import json
import re
import uuid
from urllib.parse import urlparse
from .models import IncidentModel
from lesites.models import SiteModel
import psycopg2
import pandas as pd
from requests.exceptions import ConnectTimeout, ReadTimeout, RequestException, SSLError


def get_linkedeye_connection():
    try:
        connection = psycopg2.connect(
            database=settings.POSTGRES_DB_NAME,
            user=settings.POSTGRES_USER,
            password=settings.POSTGRES_PASS,
            host=settings.POSTGRES_HOST,
            port=settings.POSTGRES_PORT
        )
        return connection
    except Exception as e:
        print(f"Error connecting to linkedeye database: {e}")
        return None


def _get_org_id_for_site(site_name):
    """
    Get the organizationId for a site.
    Matches Organization.slug = site_name (e.g., fs-le-isv, fs-dr-le).
    Falls back to incident_api field in lesite if slug match fails.
    """
    def add_candidate(candidates, value):
        value = (value or '').strip().strip('/')
        if value and value not in candidates:
            candidates.append(value)

    def add_host_candidates(candidates, value):
        value = (value or '').strip()
        if not value:
            return
        parsed = urlparse(value if '://' in value else '//' + value)
        host = (parsed.hostname or value).strip().lower()
        if not host:
            return
        add_candidate(candidates, host)
        add_candidate(candidates, host.split('.')[0])

    candidates = []
    add_candidate(candidates, site_name)

    site = None
    try:
        site = SiteModel.objects.filter(sitename=site_name, is_enable=True).first()
    except:
        site = None

    if site:
        add_candidate(candidates, getattr(site, 'sitename', ''))
        add_host_candidates(candidates, getattr(site, 'le_url', ''))
        add_host_candidates(candidates, getattr(site, 'entity_host', ''))

    try:
        conn = get_linkedeye_connection()
        if conn:
            cur = conn.cursor()
            for candidate in candidates:
                cur.execute('SELECT id FROM "Organization" WHERE slug = %s', (candidate,))
                row = cur.fetchone()
                if row:
                    cur.close()
                    conn.close()
                    return str(row[0])
            cur.close()
            conn.close()
    except:
        pass

    # Fallback: check incident_api field in lesite
    if site and site.incident_api and site.incident_api not in ('None', '', 'null'):
        return site.incident_api.strip()

    return None


def _format_relative_time(created_at):
    if not created_at:
        return 'Unknown', 'Unknown'
    try:
        dt_obj = created_at
        if isinstance(created_at, str):
            try:
                dt_obj = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            except:
                try:
                    dt_obj = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
                except:
                    return str(created_at), str(created_at)

        full_time = dt_obj.strftime("%Y-%m-%d %H:%M:%S")
        dt_naive = dt_obj.replace(tzinfo=None) if dt_obj.tzinfo else dt_obj
        now = datetime.now()
        diff = now - dt_naive

        if diff.days > 0:
            return f"{diff.days}d ago", full_time
        elif diff.seconds > 3600:
            return f"{diff.seconds // 3600}h ago", full_time
        elif diff.seconds > 60:
            return f"{diff.seconds // 60}m ago", full_time
        else:
            return "Just now", full_time
    except:
        return str(created_at), str(created_at)


@csrf_exempt
def get_incidents_api(request):
    """
    API endpoint to fetch incidents from PostgreSQL filtered by site's organizationId.
    """
    site_name = request.GET.get('site', '').strip()
    search = request.GET.get('search', '').strip().lower()
    priorities_str = request.GET.get('priorities', '')
    states_str = request.GET.get('states', '')
    assignees_str = request.GET.get('assignees', '')

    priorities = [p for p in priorities_str.split(',') if p]
    states = [s for s in states_str.split(',') if s]
    assignee_ids = [a for a in assignees_str.split(',') if a]

    page = int(request.GET.get('page', 1))
    limit = int(request.GET.get('limit', 15))
    offset = (page - 1) * limit

    try:
        conn = get_linkedeye_connection()
        if not conn:
            return JsonResponse({'status': 500, 'message': 'Database connection failed', 'incidents': []})

        cursor = conn.cursor()

        # Get organizationId for site filtering
        org_id = _get_org_id_for_site(site_name) if site_name else None

        # Resolve User mapping
        user_map = {}
        try:
            cursor.execute('SELECT id, "firstName", "lastName" FROM "User"')
            user_map = {str(r[0]): f"{r[1]} {r[2]}".strip() for r in cursor.fetchall()}
        except:
            try:
                cursor.execute('SELECT id, name FROM "User"')
                user_map = {str(r[0]): r[1] for r in cursor.fetchall()}
            except:
                pass

        # Resolve Team mapping
        team_map = {}
        try:
            cursor.execute('SELECT id, name FROM "Team"')
            team_map = {str(r[0]): r[1] for r in cursor.fetchall()}
        except:
            pass

        # Build WHERE clause
        where_clauses = []
        params = []

        # Filter by organizationId (site)
        if site_name:
            if org_id:
                where_clauses.append('"organizationId" = %s')
                params.append(org_id)
            else:
                return JsonResponse({'status': 404, 'message': f'Organization not found for site: {site_name}', 'incidents': []})
        elif org_id:
            where_clauses.append('"organizationId" = %s')
            params.append(org_id)

        if search:
            where_clauses.append('("number" ILIKE %s OR "shortDescription" ILIKE %s)')
            params.extend([f'%{search}%', f'%{search}%'])

        if priorities:
            # Cast priority column to text for comparison with string array
            where_clauses.append('"priority"::text = ANY(%s)')
            params.append(priorities)

        if states:
            # Cast state column to text for comparison with string array
            where_clauses.append('"state"::text = ANY(%s)')
            params.append(states)

        if assignee_ids:
            where_clauses.append('"assignedToId" = ANY(%s)')
            params.append(assignee_ids)

        where_sql = ""
        if where_clauses:
            where_sql = " WHERE " + " AND ".join(where_clauses)

        # Site-scoped base filter for stats (only organizationId, no search/priority filters)
        site_where = ''
        site_params = []
        if org_id:
            site_where = ' WHERE "organizationId" = %s'
            site_params = [org_id]

        # Stats — per site, not affected by search/priority filters
        cursor.execute(f"""
            SELECT
                COUNT(CASE WHEN state NOT IN ('RESOLVED', 'CLOSED', 'CANCELLED') THEN 1 END),
                COUNT(CASE WHEN priority = 'P1' AND state NOT IN ('RESOLVED', 'CLOSED', 'CANCELLED') THEN 1 END),
                COUNT(CASE WHEN priority = 'P2' AND state NOT IN ('RESOLVED', 'CLOSED', 'CANCELLED') THEN 1 END),
                COUNT(CASE WHEN "slaBreached" = true AND state NOT IN ('RESOLVED', 'CLOSED', 'CANCELLED') THEN 1 END),
                COUNT(CASE WHEN state IN ('RESOLVED', 'CLOSED') THEN 1 END)
            FROM "Incident" {site_where}
        """, site_params)
        stats_row = cursor.fetchone()
        global_stats = {
            'active': stats_row[0],
            'critical': stats_row[1],
            'high': stats_row[2],
            'sla_breached': stats_row[3],
            'resolved': stats_row[4]
        }

        # Filter options — per site
        cursor.execute(f'SELECT DISTINCT state FROM "Incident" {site_where} ORDER BY state;', site_params)
        all_states = [r[0] for r in cursor.fetchall()]

        available_assignees = []
        try:
            assignee_query = f'''
                SELECT DISTINCT u.id, u."firstName", u."lastName"
                FROM "Incident" i
                JOIN "User" u ON i."assignedToId" = u.id
                {('WHERE i."organizationId" = %s' if org_id else '')}
                ORDER BY u."firstName";
            '''
            cursor.execute(assignee_query, site_params if org_id else [])
            available_assignees = [{'id': str(r[0]), 'name': f"{r[1]} {r[2]}"} for r in cursor.fetchall()]
        except:
            pass

        # Total count with all filters
        cursor.execute(f'SELECT COUNT(*) FROM "Incident" {where_sql};', params)
        total_count = cursor.fetchone()[0]

        # Fetch paginated incidents
        data_query = f'SELECT * FROM "Incident" {where_sql} ORDER BY "createdAt" DESC LIMIT %s OFFSET %s;'
        cursor.execute(data_query, params + [limit, offset])
        colnames = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()

        incidents_list = []
        for row in rows:
            row_dict = dict(zip(colnames, row))

            assignee_id = str(row_dict.get('assignedToId', ''))
            assignee_name = user_map.get(assignee_id, 'Unassigned')

            group_id = str(row_dict.get('assignmentGroupId', ''))
            team_name = team_map.get(group_id, 'DevOps Team')

            created_at = row_dict.get('createdAt')
            relative_time, full_time = _format_relative_time(created_at)

            incident = {
                'id': row_dict.get('number', f"INC{row_dict.get('id', '???')}"),
                'priority': row_dict.get('priority', 'P3'),
                'title': row_dict.get('shortDescription', 'No Title'),
                'state': row_dict.get('state', 'New'),
                'assignee': {
                    'name': assignee_name,
                    'initials': "".join([n[0] for n in assignee_name.split() if n]) if assignee_name != 'Unassigned' else '??',
                    'team': team_name
                },
                'sla': 'Breached' if row_dict.get('slaBreached') else 'Normal',
                'time': relative_time,
                'full_time': full_time,
                'source': row_dict.get('source', 'Prometheus')
            }
            incidents_list.append(incident)

        cursor.close()
        conn.close()

        return JsonResponse({
            'status': 200,
            'incidents': incidents_list,
            'pagination': {
                'total': total_count,
                'page': page,
                'limit': limit,
                'pages': (total_count + limit - 1) // limit if total_count > 0 else 0
            },
            'stats': global_stats,
            'filter_options': {
                'states': all_states,
                'assignees': available_assignees
            }
        })

    except Exception as e:
        print(f"===Exception==get_incidents_api=== {e}")
        return JsonResponse({'status': 500, 'message': str(e), 'incidents': []})


@login_required(login_url="/")
@never_cache
def incidents(request):
    if request.method == 'GET':
        try:
            site_name = request.GET.get('site', 'default')
            context = {
                'site_name': site_name,
                'page_title': 'Incidents',
                'year': datetime.now().year,
            }
            return render(request, 'app/incidents.html', context)
        except Exception as e:
            return render(request, 'app/incidents.html', {
                'site_name': 'default',
                'page_title': 'Incidents',
                'year': datetime.now().year,
                'error': str(e)
            })

    elif request.method == 'POST':
        try:
            data = json.loads(request.body)
            site_name = request.GET.get('site', 'default')
            org_id = _get_org_id_for_site(site_name)

            conn = get_linkedeye_connection()
            if not conn:
                return JsonResponse({'status': 500, 'message': 'Database connection failed'})

            cursor = conn.cursor()

            # Generate next incident number
            cursor.execute("""
                SELECT COALESCE(MAX(CAST(REPLACE(number, 'INC', '') AS INTEGER)), 0) + 1
                FROM "Incident"
            """)
            next_num = cursor.fetchone()[0]
            new_number = f"INC{next_num:07d}"

            # Get short_description (JS sends as short_description)
            short_desc = data.get('short_description', data.get('shortDescription', ''))

            # Calculate priority from impact/urgency matrix if not provided directly
            priority = data.get('priority', '')
            if not priority:
                impact = data.get('impact', 'Enterprise')
                urgency = data.get('urgency', 'Medium')
                priority_matrix = {
                    'Enterprise': {'Critical': 'P1', 'High': 'P2', 'Medium': 'P3', 'Low': 'P4'},
                    'Department': {'Critical': 'P1', 'High': 'P2', 'Medium': 'P3', 'Low': 'P4'},
                    'Team':       {'Critical': 'P2', 'High': 'P3', 'Medium': 'P3', 'Low': 'P4'},
                    'Individual': {'Critical': 'P3', 'High': 'P4', 'Medium': 'P4', 'Low': 'P4'},
                }
                priority = priority_matrix.get(impact, {}).get(urgency, 'P3')

            new_id = str(uuid.uuid4())

            # Convert impact/urgency to UPPERCASE for PostgreSQL enum
            impact_val = data.get('impact', '').upper() or None
            urgency_val = data.get('urgency', '').upper() or None
            category_val = data.get('category', '') or None
            source_val = data.get('source', 'MANUAL').upper()

            # Get createdById — find user in PostgreSQL User table by Django username
            created_by_id = None
            try:
                django_user = request.user
                cursor.execute(
                    'SELECT id FROM "User" WHERE email = %s OR "firstName" = %s LIMIT 1',
                    (django_user.email or django_user.username, django_user.username)
                )
                row = cursor.fetchone()
                if row:
                    created_by_id = str(row[0])
            except:
                pass

            # Fallback: get any user from the org
            if not created_by_id and org_id:
                try:
                    cursor.execute('SELECT id FROM "User" WHERE "organizationId" = %s LIMIT 1', (org_id,))
                    row = cursor.fetchone()
                    if row:
                        created_by_id = str(row[0])
                except:
                    pass

            # Last fallback: get any user
            if not created_by_id:
                try:
                    cursor.execute('SELECT id FROM "User" LIMIT 1')
                    row = cursor.fetchone()
                    if row:
                        created_by_id = str(row[0])
                except:
                    pass

            cursor.execute('''
                INSERT INTO "Incident"
                (id, number, "shortDescription", description, priority, state, impact, urgency,
                 category, "slaBreached", "createdAt", "updatedAt", "organizationId", source,
                 "createdById")
                VALUES (%s, %s, %s, %s, %s, 'NEW', %s, %s, %s, false, NOW(), NOW(), %s, %s, %s)
                RETURNING id, number, "createdAt"
            ''', (
                new_id,
                new_number,
                short_desc[:200],
                data.get('description', ''),
                priority,
                impact_val,
                urgency_val,
                category_val,
                org_id,
                source_val,
                created_by_id
            ))
            result = cursor.fetchone()
            conn.commit()
            cursor.close()
            conn.close()

            return JsonResponse({
                'status': 200,
                'message': 'Incident created successfully',
                'number': result[1],
                'incident': {
                    'id': str(result[0]),
                    'number': result[1],
                    'shortDescription': short_desc,
                    'priority': priority,
                    'state': 'NEW',
                    'createdAt': result[2].isoformat() if result[2] else ''
                }
            })

        except Exception as e:
            print(f"===Exception==incidents POST=== {e}")
            return JsonResponse({'status': 500, 'message': f'Error creating incident: {str(e)}'})

    response = {'status': 405, 'msg': 'Method not allowed'}
    return HttpResponse(json.dumps(response), content_type="application/json")


@never_cache
def create_incident(request):
    if request.method == 'GET':
        site_name = request.GET.get('site', 'default')
        context = {
            'site_name': site_name,
            'page_title': 'Create Incident',
            'year': datetime.now().year,
        }
        return render(request, 'app/create_incident.html', context)

    response = {'status': 405, 'msg': 'Method not allowed'}
    return HttpResponse(json.dumps(response), content_type="application/json")


@never_cache
def incident_detail(request, incident_id):
    if request.method == 'GET':
        try:
            site_name = request.GET.get('site', 'default')

            conn = get_linkedeye_connection()
            if not conn:
                raise Exception("Database connection failed")

            cur = conn.cursor()

            query = 'SELECT id, number, "shortDescription", description, priority, state, "createdAt", "updatedAt", "slaBreached", impact, urgency, category, "assignedToId", "assignmentGroupId", "organizationId", "resolvedAt" FROM "Incident" WHERE '
            if str(incident_id).startswith('INC'):
                cur.execute(query + 'number = %s', (incident_id,))
            else:
                cur.execute(query + 'id = %s', (incident_id,))

            row = cur.fetchone()
            if not row:
                raise IncidentModel.DoesNotExist()

            class IncidentObj:
                def __init__(self, r):
                    self.id = r[0]
                    self.number = r[1]
                    self.short_description = r[2]
                    self.description = r[3]
                    self.priority = r[4]
                    self.state = r[5]
                    self.created_at = r[6]
                    self.updated_at = r[7]
                    self.sla_breached = r[8]
                    self.impact = r[9]
                    self.urgency = r[10]
                    self.category = r[11]
                    self.assigned_to_id = r[12]
                    self.assignment_group_id = r[13]
                    self.organization_id = r[14]
                    self.resolved_at = r[15]

                def get_state_display(self):
                    state_map = {'NEW': 'New', 'IN_PROGRESS': 'In Progress', 'ON_HOLD': 'On Hold', 'ESCALATED': 'Escalated', 'RESOLVED': 'Resolved', 'CLOSED': 'Closed', 'CANCELLED': 'Cancelled'}
                    return state_map.get(self.state, self.state)

            incident = IncidentObj(row)

            now_dt = datetime.now(incident.created_at.tzinfo) if incident.created_at and incident.created_at.tzinfo else datetime.now()
            elapsed_ms = int((now_dt - incident.created_at).total_seconds() * 1000) if incident.created_at else 0

            SLA_RESPONSE = {'P1': 5, 'P2': 15, 'P3': 60, 'P4': 240}
            SLA_RESOLUTION = {'P1': 60, 'P2': 240, 'P3': 1440, 'P4': 4320}

            priority = incident.priority or 'P3'
            sla_response_target = SLA_RESPONSE.get(priority, 60)
            sla_resolution_target = SLA_RESOLUTION.get(priority, 1440)

            PAGE_BG_MAP = {
              'P1': 'linear-gradient(180deg, #121212 0%, #140D0B 38%, #0F0D0C 100%)',
              'P2': 'linear-gradient(180deg, #121212 0%, #171008 38%, #0F0D0C 100%)',
              'P3': 'linear-gradient(180deg, #121212 0%, #18120B 38%, #0F0D0C 100%)',
              'P4': 'linear-gradient(180deg, #121212 0%, #12150F 38%, #0F0D0C 100%)',
            }
            PRIORITY_HERO_MAP = {
              'P1': { 'bg': 'linear-gradient(135deg, #1F1F1F 0%, #2A1412 24%, #231110 48%, #171312 74%, #121212 100%)', 'glow1': 'rgba(220,38,38,0.22)', 'glow2': 'rgba(233,145,35,0.16)', 'glow3': 'rgba(153,27,27,0.18)' },
              'P2': { 'bg': 'linear-gradient(135deg, #1F1F1F 0%, #2A1C10 24%, #24180F 48%, #1A1511 74%, #121212 100%)', 'glow1': 'rgba(233,145,35,0.24)', 'glow2': 'rgba(229,142,34,0.18)', 'glow3': 'rgba(153,96,51,0.16)' },
              'P3': { 'bg': 'linear-gradient(135deg, #1F1F1F 0%, #2A1D12 24%, #241912 48%, #1A1511 74%, #121212 100%)', 'glow1': 'rgba(233,145,35,0.24)', 'glow2': 'rgba(229,142,34,0.18)', 'glow3': 'rgba(153,96,51,0.18)' },
              'P4': { 'bg': 'linear-gradient(135deg, #1F1F1F 0%, #1E2018 24%, #1A1B15 48%, #151612 74%, #121212 100%)', 'glow1': 'rgba(16,185,129,0.18)', 'glow2': 'rgba(233,145,35,0.12)', 'glow3': 'rgba(5,150,105,0.16)' },
            }
            PRIORITY_ACCENT_MAP = {
              'P1': { 'line': 'from-red-500 via-orange-400 to-amber-500', 'glow': 'shadow-red-500/20', 'badge': 'bg-red-500/15 text-red-300 ring-red-400/30' },
              'P2': { 'line': 'from-amber-500 via-orange-400 to-amber-500', 'glow': 'shadow-amber-500/20', 'badge': 'bg-amber-500/15 text-amber-300 ring-amber-400/30' },
              'P3': { 'line': 'from-amber-500 via-orange-400 to-orange-500', 'glow': 'shadow-amber-500/20', 'badge': 'bg-orange-500/15 text-orange-300 ring-orange-400/30' },
              'P4': { 'line': 'from-emerald-500 via-teal-400 to-emerald-500', 'glow': 'shadow-emerald-500/20', 'badge': 'bg-emerald-500/15 text-emerald-300 ring-emerald-400/30' },
            }
            PRIORITY_META_MAP = {
              'P1': { 'label': 'Critical', 'bg': 'bg-red-500/15 text-red-300 ring-1 ring-red-400/30' },
              'P2': { 'label': 'High',     'bg': 'bg-amber-500/15 text-amber-300 ring-1 ring-amber-400/30' },
              'P3': { 'label': 'Medium',   'bg': 'bg-orange-500/15 text-orange-300 ring-1 ring-orange-400/30' },
              'P4': { 'label': 'Low',      'bg': 'bg-emerald-500/15 text-emerald-300 ring-1 ring-emerald-400/30' },
            }
            STATE_META_MAP = {
              'NEW':         { 'label': 'New',         'color': '#E99123', 'icon': 'circle-dot' },
              'IN_PROGRESS': { 'label': 'In Progress', 'color': '#F4B247', 'icon': 'radio' },
              'ON_HOLD':     { 'label': 'On Hold',     'color': '#D59A4A', 'icon': 'clock' },
              'ESCALATED':   { 'label': 'Escalated',   'color': '#F87171', 'icon': 'arrow-up-right' },
              'RESOLVED':    { 'label': 'Resolved',    'color': '#34D399', 'icon': 'check-circle-2' },
              'CLOSED':      { 'label': 'Closed',      'color': '#B8B1A7', 'icon': 'x-circle' },
              'CANCELLED':   { 'label': 'Cancelled',   'color': '#B8B1A7', 'icon': 'x-circle' },
            }
            SOURCE_META_MAP = {
              'PROMETHEUS': { 'icon': 'flame',       'color': 'text-amber-300',  'bg': 'bg-amber-500/15',  'label': 'Prometheus' },
              'GRAFANA':    { 'icon': 'bar-chart-3', 'color': 'text-orange-300', 'bg': 'bg-orange-500/15', 'label': 'Grafana' },
              'MANUAL':     { 'icon': 'users',       'color': 'text-stone-300',  'bg': 'bg-white/10',       'label': 'Manual' },
            }

            page_bg = PAGE_BG_MAP.get(priority, PAGE_BG_MAP['P3'])
            priority_hero = PRIORITY_HERO_MAP.get(priority, PRIORITY_HERO_MAP['P3'])
            priority_accent = PRIORITY_ACCENT_MAP.get(priority, PRIORITY_ACCENT_MAP['P3'])
            priority_meta = PRIORITY_META_MAP.get(priority, PRIORITY_META_MAP['P3'])
            state_meta = STATE_META_MAP.get(incident.state, STATE_META_MAP['NEW'])
            source_meta = SOURCE_META_MAP.get('PROMETHEUS')

            assignee_name = ''
            assignee_email = ''
            group_name = ''
            org_name = ''
            org_env = ''
            similar_incidents = []

            # Parse Description
            desc = incident.description or ''
            parsed_desc = {
                'is_alert': False, 'is_enriched': False,
                'alert_name': '', 'alert_severity': '', 'alert_client': '',
                'alert_hostname': '', 'alert_ip_instance': '',
                'sections': [], 'extra_lines': [], 'body_text': desc
            }

            if desc.startswith('Auto-created from alert:') or desc.startswith('ALERT:'):
                parsed_desc['is_alert'] = True
                lines = [l.strip() for l in desc.split('\n') if l.strip()]
                if lines:
                    parsed_desc['alert_name'] = re.sub(r'^(Auto-created from alert:|ALERT:)\s*', '', lines[0], flags=re.IGNORECASE).strip() or 'Unknown'
                pairs = {}
                for line in lines[1:]:
                    kv = re.match(r'^([A-Za-z\s_/\(\)]+):\s*(.+)$', line)
                    if kv:
                        pairs[kv.group(1).strip()] = kv.group(2).strip()
                parsed_desc['alert_severity'] = pairs.get('Severity', '')
                parsed_desc['alert_hostname'] = pairs.get('Hostname', '')
                parsed_desc['alert_ip_instance'] = pairs.get('Instance', pairs.get('IP Address', ''))
            elif desc.startswith('## Disruption Details'):
                parsed_desc['is_enriched'] = True
                sections = []
                current_sec = {'title': '', 'lines': []}
                for line in desc.split('\n'):
                    m = re.match(r'^##\s+(.+)', line)
                    if m:
                        if current_sec['title']: sections.append(current_sec)
                        current_sec = {'title': m.group(1).strip(), 'lines': []}
                    elif line.strip():
                        current_sec['lines'].append(line.strip())
                if current_sec['title']: sections.append(current_sec)

                all_pairs = {}
                for s in sections:
                    for l in s['lines']:
                        kv = re.match(r'^([A-Za-z\s_/]+):\s*(.+)$', l)
                        if kv: all_pairs[kv.group(1).strip()] = kv.group(2).strip()
                parsed_desc['alert_name'] = all_pairs.get('Alert', 'Unknown Alert')
                parsed_desc['alert_severity'] = all_pairs.get('Severity', '')
                parsed_desc['alert_hostname'] = all_pairs.get('Hostname', all_pairs.get('Host', ''))
                parsed_desc['alert_ip_instance'] = all_pairs.get('Instance', '')
            else:
                host_match = re.search(r'^Host:\s*(.+)$', desc, re.MULTILINE)
                if host_match:
                    parsed_desc['alert_hostname'] = host_match.group(1).strip()
                    parsed_desc['body_text'] = re.sub(r'^Host:\s*.+\n?', '', desc, flags=re.MULTILINE).strip()

            # Fetch enriched data
            try:
                if incident.assigned_to_id:
                    cur.execute('SELECT "firstName", "lastName", email FROM "User" WHERE id = %s', (str(incident.assigned_to_id),))
                    row = cur.fetchone()
                    if row:
                        assignee_name = f'{row[0] or ""} {row[1] or ""}'.strip()
                        assignee_email = row[2] or ''

                if incident.assignment_group_id:
                    cur.execute('SELECT name FROM "Team" WHERE id = %s', (str(incident.assignment_group_id),))
                    row = cur.fetchone()
                    if row:
                        group_name = row[0] or ''

                if incident.organization_id:
                    cur.execute('SELECT name, environment FROM "Organization" WHERE id = %s', (str(incident.organization_id),))
                    row = cur.fetchone()
                    if row:
                        org_name = row[0] or ''
                        org_env = row[1] or ''

                cur.execute('''
                    SELECT number, "shortDescription", priority, state, "resolvedAt"
                    FROM "Incident"
                    WHERE state IN ('RESOLVED', 'CLOSED')
                      AND priority = %s
                      AND id != %s
                      AND "resolvedAt" > NOW() - INTERVAL '30 days'
                    ORDER BY "resolvedAt" DESC
                    LIMIT 5
                ''', (priority, str(incident.id)))
                for r in cur.fetchall():
                    resolved_ago = ''
                    if r[4]:
                        diff_days = (datetime.now(r[4].tzinfo) - r[4]).days
                        resolved_ago = f'{diff_days}d ago' if diff_days > 0 else 'today'
                    similar_incidents.append({
                        'number': r[0], 'short_description': r[1],
                        'priority': r[2], 'state': r[3], 'resolved_ago': resolved_ago,
                    })
            except Exception as e:
                print(f"Error fetching enriched incident data: {e}")

            cur.close()
            conn.close()

            context = {
                'incident': incident,
                'page_title': f'Incident {incident.number}',
                'year': datetime.now().year,
                'site_name': site_name,
                'sla_response_target': sla_response_target,
                'sla_resolution_target': sla_resolution_target,
                'page_bg': page_bg,
                'priority_hero': priority_hero,
                'priority_accent': priority_accent,
                'priority_meta': priority_meta,
                'state_meta': state_meta,
                'source_meta': source_meta,
                'elapsed_ms': elapsed_ms,
                'parsed_desc': parsed_desc,
                'similar_incidents': similar_incidents,
                'timeline': [],
                'work_notes': [],
                'assignee_name': assignee_name,
                'assignee_initials': ''.join([w[0].upper() for w in assignee_name.split()[:2]]) if assignee_name else '',
                'assignee_email': assignee_email,
                'group_name': group_name,
                'org_name': org_name,
                'org_env': org_env,
                'priority': priority,
            }
            return render(request, 'app/incident_detail.html', context)

        except IncidentModel.DoesNotExist:
            return render(request, 'app/incident_not_found.html', {
                'page_title': 'Incident Not Found',
                'year': datetime.now().year,
            })
        except Exception as e:
            print(f"===Exception==incident_detail=== {e}")
            return render(request, 'app/incident_not_found.html', {
                'page_title': 'Incident Not Found',
                'year': datetime.now().year,
            })

    response = {'status': 405, 'msg': 'Method not allowed'}
    return HttpResponse(json.dumps(response), content_type="application/json")


@csrf_exempt
@login_required
def get_incidents_chart_data(request):
    """
    Returns time-series incident data for Chart.js.
    Supports both 'overview' (all sites aggregated) and 'siteview' (single site).
    """
    try:
        from django.db import connection
        view = request.GET.get('view', 'siteview')
        periods = int(request.GET.get('periods', 7))
        
        # Calculate date range
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=periods - 1)
        
        priorities = ['P1', 'P2', 'P3', 'P4']
        chartArray = []
        
        with connection.cursor() as cursor:
            if view == 'overview':
                # Overview: all sites aggregated breakdown by priority
                for day_offset in range(periods):
                    current_date = start_date + timedelta(days=day_offset)
                    date_str = current_date.strftime('%Y-%m-%d')
                    next_date = current_date + timedelta(days=1)
                    
                    row = [date_str]
                    for p in priorities:
                        # Query the MySQL 'incidents' table
                        cursor.execute('''
                            SELECT COUNT(*) 
                            FROM incidents 
                            WHERE created_at >= %s AND created_at < %s
                              AND priority = %s
                        ''', (current_date, next_date, p))
                        count = cursor.fetchone()[0]
                        row.append(int(count))
                    
                    chartArray.append(row)
            
            else:
                # Siteview: Single site with priority breakdown
                siteinfo = json.loads(request.GET.get('sites', '{}'))
                sitename = siteinfo.get('sitename', '')
                
                if not sitename:
                    return JsonResponse({'code': 400, 'message': 'Site name required', 'chartData': []})
                
                for day_offset in range(periods):
                    current_date = start_date + timedelta(days=day_offset)
                    date_str = current_date.strftime('%Y-%m-%d')
                    next_date = current_date + timedelta(days=1)
                    
                    row = [date_str]
                    for p in priorities:
                        cursor.execute('''
                            SELECT COUNT(*) 
                            FROM incidents 
                            WHERE site_name = %s 
                              AND created_at >= %s 
                              AND created_at < %s
                              AND priority = %s
                        ''', (sitename, current_date, next_date, p))
                        
                        count = cursor.fetchone()[0]
                        row.append(int(count))
                    
                    chartArray.append(row)
        
        return JsonResponse({
            'code': 200,
            'chartData': chartArray,
            'priorities': priorities
        })
        
    except Exception as e:
        import traceback
        print(f"===Exception==get_incidents_chart_data=== {e}")
@csrf_exempt
@login_required
def get_incidents_per_site(request):
    """
    Returns incident data for every enabled site in lesite table.
    view=timeseries  → daily totals per site for the last N periods (default 7 days)
    view=summary     → P1/P2/P3/P4 totals per site (default)
    """
    try:
        from django.db import connection

        view    = request.GET.get('view', 'summary')
        periods = int(request.GET.get('periods', 7))

        # Fetch all enabled sites from the lesite (SiteModel) table
        try:
            enabled_sites = list(
                SiteModel.objects.filter(is_enable=True)
                .values_list('sitename', flat=True)
                .order_by('sitename')
            )
        except Exception as ex:
            print(f"===get_incidents_per_site: SiteModel query failed: {ex}")
            enabled_sites = []

        if not enabled_sites:
            return JsonResponse({'code': 200, 'sites': [], 'dates': []})

        # ── TIMESERIES MODE ──────────────────────────────────────────────────────
        if view == 'timeseries':
            end_date   = datetime.now().date()
            start_date = end_date - timedelta(days=periods - 1)

            # Build ordered date labels
            dates = [
                (start_date + timedelta(days=i)).strftime('%Y-%m-%d')
                for i in range(periods)
            ]

            site_series = []
            with connection.cursor() as cursor:
                for sitename in enabled_sites:
                    day_counts = []
                    for i in range(periods):
                        current_date = start_date + timedelta(days=i)
                        next_date    = current_date + timedelta(days=1)
                        cursor.execute('''
                            SELECT COUNT(*)
                            FROM incidents
                            WHERE site_name = %s
                              AND created_at >= %s
                              AND created_at <  %s
                        ''', (sitename, current_date, next_date))
                        day_counts.append(int(cursor.fetchone()[0]))

                    # Include all sites (even zero — chart shows them in legend)
                    site_series.append({'site': sitename, 'data': day_counts})

            return JsonResponse({'code': 200, 'dates': dates, 'sites': site_series})

        # ── SUMMARY MODE (P1/P2/P3/P4) ──────────────────────────────────────────
        priorities = ['P1', 'P2', 'P3', 'P4']
        site_data  = []
        with connection.cursor() as cursor:
            for sitename in enabled_sites:
                site_row = {'site': sitename, 'P1': 0, 'P2': 0, 'P3': 0, 'P4': 0, 'total': 0}
                for p in priorities:
                    cursor.execute('''
                        SELECT COUNT(*) FROM incidents
                        WHERE site_name = %s AND priority = %s
                    ''', (sitename, p))
                    count = int(cursor.fetchone()[0])
                    site_row[p]       = count
                    site_row['total'] += count
                site_data.append(site_row)

        return JsonResponse({'code': 200, 'sites': site_data})

    except Exception as e:
        import traceback
        print(f"===Exception==get_incidents_per_site=== {e}")
        print(traceback.format_exc())
        return JsonResponse({
            'code': 500,
            'message': f'Error fetching per-site incident data: {str(e)}',
            'sites': []
        })
