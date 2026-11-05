import apprise
#import mysql.connector
#from mysql.connector import Error
import pymysql
import os
from bs4 import BeautifulSoup
from lib.LinkedEyeVault.AppSecrets import get_app_secret
from lib.LinkedEyeEntity import Node
import logging

SCRIPT_PATH= os.path.join(os.path.dirname(__file__))
class Notification(object):
    def __init__(self, connect_database=False):
        self.apprise = None
        self.response = {'status' : 400 , 'data' : "" , "error_msg" : ""}
        self.connection = None
        self.location = os.getenv('NOTIFICATION_LOG_DIR', os.path.join(SCRIPT_PATH, 'logs'))
        os.makedirs(self.location, exist_ok=True)
        if connect_database:
            self.connect_mysql()
            self.node = Node()
        self.initialize()
        logfilename = os.path.join(self.location, 'notification_service.log')
        logging.basicConfig(filename=logfilename)

    def connect_mysql(self):
        self.dbhost = os.getenv('MYSQL_DB_HOST', 'mysql.fs-linkedeye')
        self.dbport = int(os.getenv('MYSQL_DB_PORT', 3306))
        self.dbuser = os.getenv('MYSQL_DB_USER', 'root')
        self.dbpassword = (
            get_app_secret('MYSQL_DB_PASS', env_var='MYSQL_ROOT_PASSWORD', default='')
            or get_app_secret('MYSQL_DB_PASS', env_var='MYSQL_DB_PASS', default='')
        )
        self.database =  os.getenv('MYSQL_DB_NAME', 'linkedeye')
        self._connect()

    def _connect(self):
        try:
            self.connection = pymysql.connect(
                host=self.dbhost,
                port=self.dbport,
                user=self.dbuser,
                passwd=self.dbpassword,
                database=self.database
            )
            print("Connected to MySQL DB")
        except Exception as e:
            print(f"The error '{e}' occurred")
        return self

    def initialize(self):
        """Initialize fresh Apprise instance to prevent URL accumulation (CRITICAL FIX #14)."""
        asset = apprise.AppriseAsset(app_id = 'LinkedEyeNotificationService', app_desc = 'LinkedEyeNotificationService', image_url_logo = os.path.join("./", 'assets\images\Linkedeye.png'))
        self.apprise = apprise.Apprise(asset=asset, debug=True)
        #return self


    def subjectComposer(self, status, title, message):
        LECODE = {0: 'CRITICAL' , 1: 'WARNING' , 2: 'OK' , 3: 'UNKNOWN'}
        return "[ {} ] {} - {}".format(LECODE[int(status)], title, message)

    def sendAlert(self, title="", status="", message=""):
        try:
            # CRITICAL FIX #14: Reinitialize Apprise to prevent URL accumulation
            self.initialize()
            
            self.COLORCODE = {0: 'red' , 1: 'orange' , 2: 'green' , 3: 'white'}
            self.get_details(title)
            self.get_usernotification_settings(self.emailID)

            #--------------------------------------------
            SUBTABLE= message.get('subtable',{})
            if 'subtable' in message.keys():
                del message['subtable']
            variables={'COLORCODE' : self.COLORCODE[int(status)], 'TITLE': title , 'REMARKS': message, 'SUBTABLE': SUBTABLE}
            templte_file = 'templates/alert.html'
            # CRITICAL FIX #20: Use context manager for file handles
            with open(os.path.join(SCRIPT_PATH, templte_file), 'r') as data:
                file_data = data.read()
            message_body = self.get_html(variables, file_data)
            #--------------------------------------------
            with apprise.LogCapture() as logs:
                result = self.apprise.notify(body=message_body, title=self.subjectComposer(status,title,message))
                if result == True:
                    self.response["data"] = True
                    self.response["status"] = 200
                    self.response["error_msg"] = ""
                    logging.info(logs.getvalue())
                    print("Email sent")
                else:
                    self.response["data"] = False
                    self.response["status"] = 400
                    self.response["error_msg"] = "Not able send notification."
                    logging.error(logs.getvalue())
                    print("Email not sent. error : "+str(result))
            self.connection.close()
            #self.node.close()
        except Exception as ex:
            self.response["data"] = False
            self.response["status"] = 400
            self.response["error_msg"] = "LinkedEyeNotification sendnotifications : Ex =" + str(ex)
            logging.error(str(ex))
            print("Email not sent. Exception : "+str(ex))
        return self.response

    def sendnotifications(self, title="", message_format="", template_type="", variables={}, message_body=""):
        try:
            if message_body:
                message_body = message_body
            else:
                if template_type == 'monitoring':
                    templte_file = 'templates/monitoring.html'
                elif template_type == 'onboarding':
                    templte_file = 'templates/onboarding.html'
                # CRITICAL FIX #20: Use context manager for file handles
                with open(os.path.join(SCRIPT_PATH, templte_file), 'r') as data:
                    file_data = data.read()
                file_data = self.get_html(variables, file_data)
                if message_format == 'Markdown' or  message_format == 'Text':
                    message_body = self.format_message(file_data, message_format)
                if message_format == 'Html':
                    message_body = file_data
            with apprise.LogCapture() as logs:
                result = self.apprise.notify(body=message_body, title=title)
                if result == True:
                    self.response["data"] = True
                    self.response["status"] = 200
                    self.response["error_msg"] = ""
                    logging.info(logs.getvalue())
                else:
                    self.response["data"] = False
                    self.response["status"] = 400
                    self.response["error_msg"] = "Not able send notification."
                    logging.error(logs.getvalue())
        except Exception as ex:
            self.response["data"] = False
            self.response["status"] = 400
            self.response["error_msg"] = "LinkedEyeNotification sendnotifications : Ex =" + str(ex)
            logging.error(str(ex))
        return self.response

    def add_url(self, url=""):
        try:
            self.apprise.add(url+'?name=LinkedEyeNotificationService(LENS)')
        except Exception as ex:
            raise Exception("LinkedEyeNotification add_url : Ex = " + str(ex))

    def formatmessage(message="", message_format=""):
        try:
            if message_format == 'markdown':
                message_body = html2markdown.convert(message)
            if message_format == 'Text':
                h = html2text.HTML2Text()
                h.ignore_links = True
                message_body =  h.handle(message)
            if message_format == 'Html':
                message_body = message
            return message_body
        except Exception as e:
            print('========Exception===formatmessage====')
            print(str(e))
            return message

    def get_details(self,title):
        query="match (n {title:'"+str(title)+"'}) return n"
        print("get_details Query  = {}".format(query))
        for item in self.node.execute(query, ret=True):
            info = item[0]['data']
        print("get_details Output = {}".format(info))
        self.emailID = info.get('contact_email', None)

    def get_usernotification_settings(self, user, add_urls=True):
        cursor = self.connection.cursor()
        cursor.execute("select id from auth_user where (email='%s')" %(user))
        user_id = cursor.fetchone()
        if user_id == None:
            raise Exception("LinkedEyeNotification get_usernotification_settings : Ex = " + 'User '+user+ ' not found in Linkedeye.')
        else:
            cursor.execute("select * from user_notification_settings where (user_id='%s' and is_saved=%s)" %(user_id[0], True))
            service_data = self.fetchall(cursor)
            if service_data:
                if add_urls == True:
                    for service in service_data:
                        self.add_url(service['url'])
                        print("service_url = {}".format(service['url']))
                else:
                    return service_data
            else:
                raise Exception("LinkedEyeNotification get_usernotification_settings : Ex = " + 'No Preferences are saved for this user. Please login Linkedeye seve Preferences.')

    def fetchall(self, cursor):
        objs = cursor.fetchall()
        description = cursor.description
        result = []
        for obj in objs:
            i = 0
            item = {}
            while i < len(description):
                item[description[i][0]] = str(obj[i])
                i = i+1
            result.append(item)
        return result

    def get_html(self, variables={}, code=""):
        for variable in variables:
            code = code.replace("{{"+str(variable)+"}}", str(variables[variable]))
        return code

    def format_message(self, message, message_format):
        try:
            if message_format == 'Text':
                # soup = BeautifulSoup(message)
                soup = BeautifulSoup(message, "html.parser") # create a new bs4 object from the html data loaded
            for script in soup(["script", "style"]): # remove all javascript and stylesheet code
                script.extract()
            # get text
            text = soup.get_text()
            # break into lines and remove leading and trailing space on each
            lines = (line.strip() for line in text.splitlines())
            # break multi-headlines into a line each
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            # drop blank lines
            text = ' '.join(chunk for chunk in chunks if chunk)
            return text
        except Exception as ex:
            raise Exception("LinkedEyeNotification format_message : Ex = " + str(ex))
