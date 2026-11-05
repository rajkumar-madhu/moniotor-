from django.shortcuts import render
from .models import AuditlogsModel
from django.shortcuts import render,HttpResponse
import json
from django.forms.models import model_to_dict
import datetime
from django.http import JsonResponse, FileResponse
import os

# Automatically detect platform & adjust log directory
if os.name == "nt":  # Windows
    LOGS_DIR = r"C:\finalui\ui-logs"  # Use raw string or double backslashes
else:  # Linux
    LOGS_DIR = "/data/le/ui-logs"

def get_auditlogs(request):
    response = {}
    try:
        temp_list = []
        # HIGH FIX #12: Paginated query — default 500 records, max 5000
        page = int(request.GET.get('page', 1))
        page_size = min(int(request.GET.get('page_size', 500)), 5000)
        offset = (page - 1) * page_size
        log_objs = AuditlogsModel.objects.all().order_by('-created')[offset:offset + page_size]
        for log_obj in log_objs:
            obj = model_to_dict(log_obj, fields=[field.name for field in log_obj._meta.fields])
            obj['created'] = log_obj.created
            temp_list.append(obj)
        response['data'] = temp_list
        response['total'] = AuditlogsModel.objects.count()
        response['page'] = page
        response['page_size'] = page_size
        response['status'] = 200
        return HttpResponse(json.dumps(response, default=convert_timestamp), content_type="json")
    except Exception as e:
        print('--Exception---get_auditlogs---')
        print(str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))
def convert_timestamp(item_date_object):
    if isinstance(item_date_object, (datetime.date, datetime.datetime)):
        return item_date_object.timestamp()


# API to list all log files
def get_logs(request):
    """Returns a JSON response with a list of log files filtered by sitename."""
    sitename = request.GET.get("sitename", "").strip()  # Remove leading/trailing spaces

    if not os.path.exists(LOGS_DIR):
        return JsonResponse({"status": 500, "error": f"Directory {LOGS_DIR} not found."})

    try:
        # Get all .txt files matching sitename
        files = [f for f in os.listdir(LOGS_DIR) if f.endswith(".txt") and sitename in f]
        return JsonResponse({"status": 200, "files": files})
    except Exception as e:
        return JsonResponse({"status": 500, "error": str(e)})

# View to render logs on a webpage
def view_logs(request):
    """Renders a webpage showing available log files."""
    if not os.path.exists(LOGS_DIR):
        return render(request, "logs.html", {"error": "Directory not found.", "logs": []})

    try:
        logs = [f for f in os.listdir(LOGS_DIR) if f.endswith(".txt")]
        return render(request, "logs.html", {"logs": logs})
    except Exception as e:
        return render(request, "logs.html", {"error": str(e), "logs": []})

# API to fetch a specific log file
def get_log_file(request, filename):
    """Serves a log file for download/viewing."""
    file_path = os.path.join(LOGS_DIR, filename)

    # Prevent directory traversal (e.g., filename="../../etc/passwd")
    if ".." in filename or "/" in filename or "\\" in filename:
        return JsonResponse({"status": 400, "error": "Invalid filename."})

    if not os.path.exists(file_path):
        return JsonResponse({"status": 404, "error": "File not found"})

    try:
        return FileResponse(open(file_path, "rb"), content_type="text/plain")
    except Exception as e:
        return JsonResponse({"status": 500, "error": str(e)})