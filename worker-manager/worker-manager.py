import json, os, time, random, string
from functions.db_functions import *
from functions.rabbit_functions import *
from functions.docker_functions import *
from functions.server_functions import *
from bson.json_util import dumps, loads
from threading import Thread
from random import randint


# read config file at startup
# load the login params from auth.json file
print "reading conf.json file"
auth_file = json.load(open("conf.json"))
registry_auth_user = auth_file["registry_auth_user"]
registry_auth_password = auth_file["registry_auth_password"]
registry_host = auth_file["registry_host"]
rabbit_host = auth_file["rabbit_host"]
rabbit_vhost = auth_file["rabbit_vhost"]
rabbit_port = auth_file["rabbit_port"]
rabbit_user = auth_file["rabbit_user"]
rabbit_password = auth_file["rabbit_password"]
mongo_url = auth_file["mongo_url"]
schema_name = auth_file["schema_name"]
max_restart_wait_in_seconds = auth_file["max_restart_wait_in_seconds"]

# get the app name the worker manages
app_name = os.environ["APP_NAME"]

# get number of cpu cores on host
cpu_cores = get_number_of_cpu_cores()

# work against docker socket
cli = Client(base_url='unix://var/run/docker.sock', version="auto")

def split_container_name_version(image_name):
    try:
        image_registry_name, image_name = image_name.rsplit("/", 1)
    except:
        image_registry_name = "registry.hub.docker.com/library"
    try:
        image_name, version_name = image_name.split(":")
    except:
        version_name = "latest"
    try:
        image_name = image_registry_name + "/" + image_name
    except:
        pass
    return image_registry_name, image_name, version_name

def randomword(length):
    return ''.join(random.choice(string.lowercase) for i in range(length))


# login to rabbit function
def rabbit_login():
    rabbit_connection = rabbit_connect(rabbit_user, rabbit_password, rabbit_host, rabbit_port, rabbit_vhost)
    rabbit_connection_channel = rabbit_create_channel(rabbit_connection)
    return rabbit_connection_channel


# update\release\restart function
def roll_containers(app_json):
    image_registry_name, image_name, version_name = split_container_name_version(app_json["docker_image"])
    # wait between zero to max_restart_wait_in_seconds seconds before rolling - avoids overloading backend
    time.sleep(randint(0, max_restart_wait_in_seconds))
    #pull image to speed up downtime between stop & start
    pull_image(image_name, version_tag=version_name, registry_user=registry_auth_user,
               registry_pass=registry_auth_password, registry_host=registry_host)
    # stop running containers
    stop_containers(app_json)
    # start new containers
    start_containers(app_json, no_pull=True)
    return


# stop app function
def stop_containers(app_json):
    # list current containers
    containers_list = list_containers(app_name)
    # stop running containers
    threads = []
    for container in containers_list:
        t = Thread(target=stop_and_remove_container, args=(container["Id"],))
        threads.append(t)
        t.start()
    for z in threads:
        z.join()
    return


# start app function
def start_containers(app_json, no_pull=False):
    # list current containers
    split_container_name_version(app_json["docker_image"])
    containers_list = list_containers(app_name)
    if len(containers_list) > 0:
        print "app already running so restarting rather then starting containers"
        roll_containers(app_json)
    else:
        # find out how many containers needed
        image_registry_name, image_name, version_name = split_container_name_version(app_json["docker_image"])
        containers_needed = cpu_cores * app_json["containers_per_cpu"]
        #pull latest image
        if no_pull is False:
            pull_image(image_name, version_tag=version_name, registry_user=registry_auth_user,
                       registry_pass=registry_auth_password, registry_host=registry_host)
        # start new containers
        container_number = 1
        threads = []
        while container_number <= containers_needed:
            port_binds = dict()
            for x in app_json["starting_ports"]:
                port_binds[x] = x + container_number
            t = Thread(target=run_container, args=(app_name, app_name + str(container_number),
                                                         image_name, port_binds,
                                                         app_json["starting_ports"], app_json["env_vars"], version_name,
                                                         registry_auth_user, registry_auth_password))
            threads.append(t)
            t.start()
            #run_container(app_name, app_name + str(container_number), app_json["docker_image"],port_binds,app_json["starting_ports"], app_json["env_vars"], version_tag="latest", docker_registry_user=registry_auth_user,docker_registry_pass=registry_auth_password)
            container_number = container_number + 1
        for y in threads:
            y.join()
        return

def rabbit_work_function(ch, method, properties, body):
    try:
        # check the message body to get the needed order
        app_json = loads(body)
        # if it's blank stop containers and kill worker-manger container
        if len(app_json) == 0:
            stop_containers(app_json)
            exit(2)
        # elif it's stopped stop containers
        elif app_json["command"] == "stop":
            stop_containers(app_json)
        # if it's start start containers
        elif app_json["command"] == "start":
            start_containers(app_json)
        # elif restart containers
        else:
            roll_containers(app_json)
        # ack message
        rabbit_ack(ch, method)
    except pika.exceptions.ConnectionClosed:
        print "lost rabbitmq connection mid transfer - dropping container to be on the safe side"
        exit(2)


def rabbit_recursive_connect(rabbit_channel, rabbit_work_function, rabbit_queue_name):
    try:
        rabbit_receive(rabbit_channel, rabbit_work_function, rabbit_queue_name)
    except pika.exceptions.ConnectionClosed:
        print "lost rabbitmq connection - reconnecting"
        rabbit_channel = rabbit_login()
        try:
            rabbit_bind_queue(rabbit_queue_name, rabbit_channel, str(app_name) + "_fanout")
            time.sleep(1)
        except pika.exceptions.ChannelClosed:
            print "queue no longer exists - can't guarantee order so dropping container"
            exit(2)
        rabbit_recursive_connect(rabbit_channel, rabbit_work_function, rabbit_queue_name)


# connect to rabbit and create queue first thing at startup
rabbit_channel = rabbit_login()
rabbit_queue_name = str(app_name) + "_" + randomword(10) + "_queue"
rabbit_queue = rabbit_create_queue(rabbit_queue_name, rabbit_channel)
rabbit_bind_queue(rabbit_queue_name, rabbit_channel, str(app_name) + "_fanout")

# at startup connect to db, load newest app image and restart containers if configured to run
mongo_collection = mongo_connect_get_app_data_disconnect(mongo_url, app_name, schema_name="nebula")
# check if app is set to running state
if mongo_collection["running"] is True:
    # if answer is yes start it
    roll_containers(mongo_collection)
# start processing rabbit queue
rabbit_recursive_connect(rabbit_channel, rabbit_work_function, rabbit_queue_name)
