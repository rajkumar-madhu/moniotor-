import sys, os, json, ast
import yaml
from django.shortcuts import render
from django.urls import reverse
from json import dumps as jdumps
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.http import HttpResponse, HttpRequest
from django.contrib.auth.models import User
from django.db import connection
from lib.LinkedEyeValidation import *
from lesites.models import SiteModel
from useronboard.models import Usersite
from lib.LinkedEyeEntity.Node import Node
import pandas as pd
import numpy as np
import re


def configmapCompare(request):
    try:
        response = {}
        type = request.GET.get("type")
        if request.GET["site"] == '':
            user_id = User.objects.get(username=request.user).id
            siteid_list = Usersite.objects.filter(user_id=user_id).values_list('site_id', flat=True)
            site_ids = [x for x in siteid_list]
            site_objs = SiteModel.objects.filter(id__in=site_ids, is_enable=True)
        else:
            site = request.GET["site"]
            site_objs = SiteModel.objects.filter(sitename=site)
        filter_json = json.loads(request.GET["filter_json"])
        query_created="MATCH (n:"+type+") RETURN n"
        if filter_json:
            keys_created = []
            for key, values in filter_json.items():
                if key=='site':
                    site_objs = SiteModel.objects.filter(sitename__in=values, is_enable=True)
                else:
                    value_conditions = []
                    for value in values:
                        value_condition = "n." + str(key) + "='" + str(value) + "'"
                        value_conditions.append(value_condition)
                    key_condition = "(" + " OR ".join(value_conditions) + ")"
                    keys_created.append(key_condition)
            keys_combined = " AND ".join(keys_created)
            if(keys_combined):
                query_created = "MATCH (n:"+type+") WHERE " + keys_combined + " RETURN n"

        print('============================this is query_created====================================')
        print(query_created)
        data = []
        sites = []
        empty_sites = []
        temp_data_array = 0
        for site_obj in site_objs:
            temp_obj = {}
            temp_obj['site'] = site_obj.sitename
            #sites.append(site_obj.sitename)
            try:
                nodeObj = Node(host=site_obj.entity_host, port=site_obj.entity_port, secure=site_obj.is_URLSecured)
                node_res = nodeObj.getcomparisiondata(type=type,query_created=query_created)
                #node_res = nodeObj.getcomparisiondata(type=type,query_created=query_created)
                temp_data_array = node_res['data']
                if (len(temp_data_array)):
                    sites.append(site_obj.sitename)
                else:
                    empty_sites.append(site_obj.sitename)
                for dictionary in temp_data_array:
                    dictionary['site'] = site_obj.sitename
            except Exception as e:
                temp_obj['site_data'] = {}
                temp_obj["code"] = 500
            data.append(temp_data_array)
        if len(data) > 1:
            data = np.concatenate(data)
            data = data.tolist()
        else:
            data = data[0]

        response["status"] = 200
        # Extract unique sites and names from the data
        names = list(set(item["name"] for item in data))

        # Generate a list of dictionaries for missing names in each site
        missing_names = [
            {"site": site, "name": name}
            #{"site": site, "name": name, "present_status": "not present"}
            for site in sites
            for name in names
            if name not in [item["name"] for item in data if item["site"] == site]
        ]
        empty_sites_names=[
            {"site": site, "name": 'No config-map available', "namespace": 'No namespace available'}
            for site in empty_sites
            ]
        # Update the data with the missing name dictionaries
        data.extend(missing_names)
        data.extend(empty_sites_names)
        #print('===============================================================THE BELOW IS DATA ARRAY=========================================================')
        #print(data)

        # Create a DataFrame from the data
        df = pd.DataFrame(data)
        #print('============================this is df Initial====================================')
        #print(df)
        df.set_index('site', inplace=True)
        df = df.transpose()
        
        # Fill empty values with "NaN"
        df.fillna('NaN', inplace=True)

        # Sort rows with "name" and "namespace" to be displayed at the top
        #df = df.reindex(index=['name', 'namespace']).concat(df.drop(['name', 'namespace'], axis=0))
        # Reindex the DataFrame
        df_reindexed = df.reindex(index=['name', 'namespace'])

        # Drop the 'name' and 'namespace' columns from the original DataFrame
        df_dropped = df.drop(['name', 'namespace'], axis=0)

        # Concatenate the reindexed DataFrame with the dropped DataFrame
        df = pd.concat([df_reindexed,df_dropped])
        #df = pd.concat([df_dropped, df_reindexed])
        

        # Create the HTML string with row heading values and select dropdowns
        df_html = "<table border='1' class='dataframe'>\n"
        df_html += "<thead>\n"
        df_html += "<tr style='text-align: right;'>\n"

        # Generate the column headings with select dropdown for 'site' column
        df_html += "<th style='display:flex;position: sticky;left: 0;z-index: 2;'><div style='width:60% !important'>site</div>" \
                   "<select class='table-filter mul-select' multiple='true' autocomplete='new-password' onchange='filter_rows()' col-index=0>" \
                   "<option value='all'></option>"

        # Get unique values from column headings for the 'site' column dropdown
        unique_sites = pd.unique(df.columns)
        for site in unique_sites:
            # Check if the site is selected in filter_json
            selected = "selected" if site in filter_json.get("site", []) else ""
            df_html += "<option value='" + str(site) + "' " + selected + ">" + str(site) + "</option>"

        df_html += "</select></th>\n"

        
        # List to store column styles
        column_styles = [""] * len(df.columns)

        for i, column in enumerate(df.columns):
            if column == 'site':
                continue

            # Check if any cells in the column have a red background color
            if any(df[column] == "NaN"):
                # Set the column header style in the site row to red
                column_styles[i] = "style='background-color: red;'"

            df_html += "<th " + column_styles[i] + " col-index=" + str(i + 1) + ">" + str(column) + "</th>\n"

        df_html += "</tr>\n"
        df_html += "</thead>\n"
        df_html += "<tbody>\n"

        # Generate each row with row heading value and select dropdowns
        for row in df.itertuples():
            df_html += "<tr>\n"

            if row[0] in ['name', 'namespace']:
                # Add the col-index and select dropdown for 'name' and 'namespace' columns in each row
                df_html += "<th style='position: sticky;left: 0;z-index: 2;" + column_styles[0] + "'>" + str(row[0]) + "<select class='table-filter' onchange='filter_rows()'>" \
                                                   "<option value='all'></option>\n"
                try:
                    unique_values = pd.unique(row[1:])
                    unique_values = unique_values[(unique_values != "NaN") & (unique_values != "No config-map available") & (unique_values != "No namespace available")]
                    unique_values = np.sort(unique_values)
                except Exception as e:
                    response['status'] = 400
                    response['msg'] = 'Not able to logout'
        
                for value in unique_values:
                    # Check if the value is selected in filter_json
                    selected = "selected" if str(value) in filter_json.get(str(row[0]), []) else ""
                    df_html += "<option value='" + str(value) + "' " + selected + ">" + str(value) + "</option>\n"
                df_html += "</select></th>\n"
            else:
                df_html += "<th style='position: sticky;left: 0;z-index: 2;'>" + str(row[0]) + "</th>"

            for value in row[1:]:
                if value == "NaN":
                    df_html += "<td style='background-color: red; font-weight: 400;'>" + str(value) + "</td>\n"
                else:
                    df_html += "<td>" + str(value).replace('\n', '<br>') + "</td>\n"

            df_html += "</tr>\n"

        df_html += "</tbody>\n"
        df_html += "</table>"

        # Display the modified HTML output
        # print(df_html)

    except Exception as e:
        response['status'] = 400
        response['msg'] = 'Error while comparing'

    return HttpResponse(df_html)


def versionCompare(request):
    try:
        response = {}
        type = request.GET.get("type")
        
        ####################### MASTER BLOCK START ####################

        mastersite = request.GET.get("master")
        mastersite_obj = SiteModel.objects.filter(sitename=mastersite)
        m_data = []
        empty_sites = []
        m_temp_data_array = 0
        m_query="MATCH (n:gitversion) RETURN n "
        #m_query="MATCH (n:gitversion) RETURN n"
        for m_site_obj in mastersite_obj:
            temp_obj = {}
            temp_obj['site'] = m_site_obj.sitename
            #sites.append(site_obj.sitename)
            try:
                nodeObj = Node(host=m_site_obj.entity_host, port=m_site_obj.entity_port, secure=m_site_obj.is_URLSecured)
                node_res = nodeObj.getcomparisiondata(type=type,query_created=m_query)
                m_temp_data_array = node_res['data']
                for dictionary in m_temp_data_array:
                    dictionary['site'] = m_site_obj.sitename
            except Exception as e:
                temp_obj['site_data'] = {}
                temp_obj["code"] = 500
            m_data.append(m_temp_data_array)
        if len(m_data) > 1:
            m_data = np.concatenate(data)
            m_data = m_data.tolist()
        else:
            m_data = m_data[0]
        #print('M_DATA--->{}'.format(m_data))
        ######################## MASTER BLOCK END ####################
        if request.GET["site"] == '':
            user_id = User.objects.get(username=request.user).id
            siteid_list = Usersite.objects.filter(user_id=user_id).values_list('site_id', flat=True)
            site_ids = [x for x in siteid_list]
            site_objs = SiteModel.objects.filter(id__in=site_ids, is_enable=True)
        else:
            site = request.GET["site"]
            site_objs = SiteModel.objects.filter(sitename=site)
        filter_json = json.loads(request.GET["filter_json"])
        query_created="MATCH (n:siteversion) RETURN n"
        #query_created="MATCH (n:site_version) RETURN n"
        if filter_json:
            keys_created = []
            for key, values in filter_json.items():
                if key=='site':
                    site_objs = SiteModel.objects.filter(sitename__in=values, is_enable=True)
                else:
                    value_conditions = []
                    for value in values:
                        value_condition = "n." + str(key) + "='" + str(value) + "'"
                        value_conditions.append(value_condition)
                    key_condition = "(" + " OR ".join(value_conditions) + ")"
                    keys_created.append(key_condition)
            keys_combined = " AND ".join(keys_created)
            if(keys_combined):
                query_created = "MATCH (n:site_version) WHERE " + keys_combined + " RETURN n"

        #print('============================this is query_created====================================')
        print(query_created)
        data = []
        sites = []
        empty_sites = []
        temp_data_array = 0
        for site_obj in site_objs:
            temp_obj = {}
            temp_obj['site'] = site_obj.sitename
            #sites.append(site_obj.sitename)
            try:
                nodeObj = Node(host=site_obj.entity_host, port=site_obj.entity_port, secure=site_obj.is_URLSecured)
                node_res = nodeObj.getcomparisiondata(type=type,query_created=query_created)
                #node_res = nodeObj.getcomparisiondata(type=type,query_created=query_created)
                temp_data_array = node_res['data']
                if (len(temp_data_array)):
                    sites.append(site_obj.sitename)
                else:
                    empty_sites.append(site_obj.sitename)
                for dictionary in temp_data_array:
                    dictionary['site'] = site_obj.sitename
            except Exception as e:
                temp_obj['site_data'] = {}
                temp_obj["code"] = 500
            data.append(temp_data_array)
        if len(data) > 1:
            data = np.concatenate(data)
            data = data.tolist()
        else:
            data = data[0]
        #print('DATA--->{}'.format(data))
        response["status"] = 200
        
        html = generate_version_html(m_data, data)
        # Display the modified HTML output
        # print(df_html)
        return HttpResponse(html)
    except Exception as e:
        response['status'] = 400
        response['msg'] = 'Error while comparing'
        return response


def generate_version_html(m_data, data):#This below function groups the function based on the  titles with same imagename
    m_df = pd.DataFrame(m_data)
    df = pd.DataFrame(data)
    m_df=m_df.replace('__', '-', regex=True)
    df=df.replace('__', '-', regex=True)
    all_titles = list(set(df['title']))
    all_sites = list(set(df['site']))

    columns = ['Title', 'Image Name', 'GIT'] + all_sites

    html = "<table border='1'>\n"
    html += "<tr>\n"
    for i, column in enumerate(columns):
        if i == 0:
            html += f"<th class='fixed-column'>{column}</th>\n"
        else:
            html += f"<th>{column}</th>\n"

    html += "</tr>\n"

    unique_images = list(set(df['imagename']))

    for image in unique_images:
        image_data = df[df['imagename'] == image]
        html += f"<tr><td class='fixed-column'>{image_data['title'].iloc[0]}</td>"
    
        html += f"<td rowspan='{len(image_data)}'>{image}</td>"
        version_data = m_df[m_df['imagename'] == image]['version'].values[0] if not m_df[m_df['imagename'] == image].empty else '-'
        html += f"<td>{version_data}</td>"
        
        current_site = image_data['site'].iloc[0]
        for site in all_sites:
            site_data = image_data[image_data['site'] == site]
            if not site_data.empty and site == current_site:
                html += f"<td>{version_data}</td>"
            else:
                html += "<td>-</td>"

        html += "</tr>"

        for i in range(1, len(image_data)):
            current_site = image_data['site'].iloc[i]
            html += f"<tr><td class='fixed-column'>{image_data['title'].iloc[i]}</td><td>{version_data}</td>"
            for _ in all_sites:
                if _ == current_site:
                    html += f"<td>{version_data}</td>"
                else:
                    html += "<td>-</td>"
            html += "</tr>"

    html += "</table>"
    return html



"""#This below function groups the function based on the same titles with same imagename
def generate_html(m_data, data):

    m_df = pd.DataFrame(m_data)

    df = pd.DataFrame(data)
 
    all_titles = list(set(df['title']))

    all_sites = list(set(df['site']))
 
    columns = ['Title', 'Image Name', 'GIT'] + all_sites
 
    html = "<table border='1'>\n"

    html += "<tr>\n"

    for column in columns:

        html += f"<th>{column}</th>\n"

    html += "</tr>\n"
 
    for title in all_titles:

        title_data = df[df['title'] == title]

        unique_images = list(set(title_data['imagename']))
 
        for image in unique_images:

            image_data = title_data[title_data['imagename'] == image]

            print('IMAGE_DATA---- {}'.format(image_data))

            print('TITLE--{} and IMAGE--{} and LENGTH OF IMAGEDATA--{}'.format(title,image,len(image_data)))

            html += f"<tr><td>{title}</td>"

            html += f"<td rowspan='{len(image_data)}'>{image}</td>"

            version_data = m_df[m_df['imagename'] == image]['version'].values[0] if not m_df[m_df['imagename'] == image].empty else '-'

            #version_data = m_df[m_df['imagename'] == image]['version'].iloc[0]

            html += f"<td>{version_data}</td>"

            print('IMAGE_DATA [0][site]---- {}'.format(image_data['site'].iloc[0]))

            current_site=image_data['site'].iloc[0]

            for site in all_sites:

                site_data = image_data[image_data['site'] == site]

                if not site_data.empty and site==current_site:

                    #if not site_data.empty and (image_data[image_data['site']==()]):

                    html += f"<td>{version_data}</td>"

                else:

                    html += "<td>-</td>"
 
            html += "</tr>"
 
            for i in range(1, len(image_data)):

                html += f"<tr><td>{image_data['title'].iloc[i]}</td><td>{version_data}</td>"

                current_site=image_data['site'].iloc[i]

                #html += f"<tr><td></td><td>{image_data['imagename'].iloc[i]}</td><td>{version_data}</td>"

                for _ in all_sites:

                    if _==current_site:

                        #if not site_data.empty and (image_data[image_data['site']==()]):

                        html += f"<td>{version_data}</td>"

                    else:

                        html += "<td>-</td>"

                html += "</tr>"
 
    html += "</table>"
 
    return html


def versionCompareTest(request):

    html_table = generate_version_html(m_data, data)
    print(html_table)
    return HttpResponse(html_table)

"""        