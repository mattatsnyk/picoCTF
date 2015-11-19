"""
Problem deployment.
"""

PROBLEM_FILES_DIR = "problem_files"
STATIC_FILE_ROOT = "static"
SYSTEMD_SERVICE_PATH = "/etc/systemd/system/"

# will be set to the configuration module during deployment
deploy_config = None

port_map = {}
current_problem = None
current_instance = None

def get_deploy_context():
    """
    Returns the deployment context, a dictionary containing the current
    config, port_map, problem, instance
    """

    global deploy_config, port_map, current_problem, current_instance

    return {
        "config": deploy_config,
        "port_map": port_map,
        "problem": current_problem,
        "instance": current_instance
    }


port_random = None

def give_port():
    """
    Returns a random port and registers it.
    """

    global port_random

    context = get_deploy_context()

    # default behavior
    if context["config"] is None:
        return randint(1000, 65000)

    # during real deployment, let's register a port
    if port_random is None:
        port_random = Random(context["config"].DEPLOY_SECRET)

    if len(context["port_map"].items()) + len(context["config"].BANNED_PORTS) == 65536:
        raise Exception("All usable ports are taken. Cannot deploy any more instances.")

    while True:
        port = port_random.randint(0, 65535)
        if port not in context["config"].BANNED_PORTS:
            owner, instance = context["port_map"].get(port, (None, None))
            if owner is None or (owner == context["problem"] and instance == context["instance"]):
                context["port_map"][port] = (context["problem"], context["instance"])
                return port

from os.path import join, isdir
from random import Random, randint
from abc import ABCMeta
from hashlib import md5
from imp import load_source
from pwd import getpwnam
from grp import getgrnam
from time import sleep
from copy import copy, deepcopy
from spur import RunProcessError
from jinja2 import Environment, Template, FileSystemLoader
from hacksport.problem import Remote, Compiled, Service, FlaskApp, PHPApp
from hacksport.problem import File, ProtectedFile, ExecutableFile
from hacksport.operations import create_user, execute
from hacksport.status import get_all_problems, get_all_problem_instances
from shell_manager.bundle import get_bundle

from shell_manager.bundle import get_bundle, get_bundle_root
from shell_manager.problem import get_problem, get_problem_root
from shell_manager.util import HACKSPORTS_ROOT, STAGING_ROOT, DEPLOYED_ROOT, sanitize_name, get_attributes
from shell_manager.util import FatalException

import os, json, shutil, logging
import functools, traceback

logger = logging.getLogger(__name__)

def challenge_meta(attributes):
    """
    Returns a metaclass that will introduce the given attributes into the class
    namespace.

    Args:
        attributes: The dictionary of attributes

    Returns:
        The metaclass described above
    """

    class ChallengeMeta(ABCMeta):
        def __new__(cls, name, bases, attr):
            attrs = dict(attr)
            attrs.update(attributes)
            return super().__new__(cls, name, bases, attrs)
    return ChallengeMeta

def update_problem_class(Class, problem_object, seed, user, instance_directory):
    """
    Changes the metaclass of the given class to introduce necessary fields before
    object instantiation.

    Args:
        Class: The problem class to be updated
        problem_name: The problem name
        seed: The seed for the Random object
        user: The linux username for this challenge instance
        instance_directory: The deployment directory for this instance

    Returns:
        The updated class described above
    """

    random = Random(seed)
    attributes = deepcopy(problem_object)

    attributes.update({"random": random, "user": user, "default_user": deploy_config.DEFAULT_USER,
                       "server": deploy_config.HOSTNAME, "directory": instance_directory})

    return challenge_meta(attributes)(Class.__name__, Class.__bases__, Class.__dict__)


def get_username(problem_name, instance_number):
    """
    Determine the username for a given problem instance.
    """

    return "{}_{}".format(sanitize_name(problem_name), instance_number)

def create_service_file(problem, instance_number, path):
    """
    Creates a systemd service file for the given problem

    Args:
        problem: the instantiated problem object
        instance_number: the instance number
        path: the location to drop the service file
    Returns:
        The path to the created service file
    """

    template = """[Unit]
Description={} instance

[Service]
User={}
WorkingDirectory={}
Type={}
ExecStart={}
Restart={}

[Install]
WantedBy=shell_manager.target
"""

    problem_service_info = problem.service()
    content = template.format(problem.name, problem.user, problem.directory,
                              problem_service_info['Type'], problem_service_info['ExecStart'],
                              "no" if problem_service_info['Type'] == "oneshot" else "always")
    service_file_path = join(path, "{}.service".format(problem.user))

    with open(service_file_path, "w") as f:
        f.write(content)

    return service_file_path

def create_instance_user(problem_name, instance_number):
    """
    Generates a random username based on the problem name. The username returned is guaranteed to
    not exist.

    Args:
        problem_name: The name of the problem
        instance_number: The unique number for this instance
    Returns:
        A tuple containing the username and home directory
    """

    converted_name = sanitize_name(problem_name)
    username = get_username(converted_name, instance_number)

    try:
        #Check if the user already exists.
        user = getpwnam(username)
        return username, user.pw_dir

    except KeyError:
        problem_home = deploy_config.HOME_DIRECTORY_ROOT

        if deploy_config.OBFUSCATE_PROBLEM_DIRECTORIES:
            secret = md5((deploy_config.DEPLOY_SECRET + username).encode()).hexdigest()
            problem_home = join(problem_home, secret) 

        home_directory = create_user(username, home_directory_root=problem_home)

        return username, home_directory

def generate_seed(*args):
    """
    Generates a seed using the list of string arguments
    """

    return md5("".join(args).encode("utf-8")).hexdigest()

def generate_staging_directory(root=STAGING_ROOT):
    """
    Creates a random, empty staging directory

    Args:
        root: The parent directory for the new directory. Defaults to join(HACKSPORTS_ROOT, "staging")

    Returns:
        The path of the generated directory
    """

    if not os.path.isdir(root):
        os.makedirs(root)

    # ensure that the staging files are not world-readable
    os.chmod(root, 0o750)

    def get_new_path():
        path = join(root, str(randint(0, 1e12)))
        if os.path.isdir(path):
            return get_new_path()
        return path

    path = get_new_path()
    os.makedirs(path)
    return path

def template_string(template, **kwargs):
    """
    Templates the given string with the keyword arguments

    Args:
        template: The template string
        **kwards: Variables to use in templating
    """

    temp = Template(template)
    return temp.render(**kwargs)

def template_file(in_file_path, out_file_path, **kwargs):
    """
    Templates the given file with the keyword arguments.

    Args:
        in_file_path: The path to the template
        out_file_path: The path to output the templated file
        **kwargs: Variables to use in templating
    """

    env = Environment(loader=FileSystemLoader(os.path.dirname(in_file_path)), keep_trailing_newline=True)
    template = env.get_template(os.path.basename(in_file_path))
    output = template.render(**kwargs)

    with open(out_file_path, "w") as f:
        f.write(output)

def template_staging_directory(staging_directory, problem, dont_template_files = ["problem.json", "challenge.py"],
                                                           dont_template_directories = ["templates"]):
    """
    Templates every file in the staging directory recursively other than
    problem.json and challenge.py.

    Args:
        staging_directory: The path of the staging directory
        problem: The problem object
        dont_template_files: The list of files not to template. Defaults to ["problem.json", "challenge.py"]
        dont_template_directories: The list of files not to recurse into. Defaults to ["templates"]
    """

    # prepend the staging directory to all
    dont_template_directories = [join(staging_directory, directory) for directory in dont_template_directories]

    for root, dirnames, filenames in os.walk(staging_directory):
        if root in dont_template_directories:
            continue
        for filename in filenames:
            if filename in dont_template_files:
                continue
            fullpath = join(root, filename)
            try:
                template_file(fullpath, fullpath, **get_attributes(problem))
            except UnicodeDecodeError as e:
                # tried templating binary file
                pass

def deploy_files(staging_directory, instance_directory, file_list, username, problem_class):
    """
    Copies the list of files from the staging directory to the instance directory.
    Will properly set permissions and setgid files based on their type.
    """

    # get uid and gid for default and problem user
    user = getpwnam(username)
    default = getpwnam(deploy_config.DEFAULT_USER)

    for f in file_list:
        # copy the file over, making the directories as needed
        output_path = join(instance_directory, f.path)
        if not os.path.isdir(os.path.dirname(output_path)):
            os.makedirs(os.path.dirname(output_path))
        shutil.copy2(join(staging_directory, f.path), output_path)

        # set the ownership based on the type of file
        if isinstance(f, ProtectedFile) or isinstance(f, ExecutableFile):
            os.chown(output_path, default.pw_uid, user.pw_gid)
        else:
            uid = default.pw_uid if f.user is None else getpwnam(f.user).pw_uid
            gid = default.pw_gid if f.group is None else getgrnam(f.group).gr_gid
            os.chown(output_path, uid, gid)

        # set the permissions appropriately
        os.chmod(output_path, f.permissions)

    if issubclass(problem_class, Service):
        os.chown(instance_directory, default.pw_uid, user.pw_gid)
        os.chmod(instance_directory, 0o750)

def install_user_service(service_file):
    """
    Installs the service file into the systemd service directory,
    sets the service to start on boot, and starts the service now.

    Args:
        service_file: The path to the systemd service file to install
    """

    service_name = os.path.basename(service_file)

    logger.debug("...Installing user service '%s'.", service_name)

    # copy service file
    service_path = os.path.join(SYSTEMD_SERVICE_PATH, service_name)
    shutil.copy2(service_file, service_path)

    execute(["systemctl", "daemon-reload"], timeout=60)
    execute(["systemctl", "enable", service_name], timeout=60)
    execute(["systemctl", "restart", service_name], timeout=60)

def generate_instance(problem_object, problem_directory, instance_number,
                      staging_directory, deployment_directory=None):
    """
    Runs the setup functions of Problem in the correct order

    Args:
        problem_object: The contents of the problem.json
        problem_directory: The directory to the problem
        instance_number: The instance number to be generated
        staging_directory: The temporary directory to store files in
        deployment_directory: The directory that will be deployed to. Defaults to the home directory of the user created.

    Returns:
        A tuple containing (problem, staging_directory, home_directory, files)
    """

    logger.debug("Generating instance %d of problem '%s'.", instance_number, problem_object["name"])

    username, home_directory = create_instance_user(problem_object['name'], instance_number)
    logger.debug("...Created problem user '%s' at '%s'.", username, home_directory)

    seed = generate_seed(problem_object['name'], deploy_config.DEPLOY_SECRET, str(instance_number))
    logger.debug("...Generated random seed '%s' for deployment.", seed)

    copypath = join(staging_directory, PROBLEM_FILES_DIR)
    shutil.copytree(problem_directory, copypath)

    # store cwd to restore later
    cwd = os.getcwd()
    os.chdir(copypath)

    challenge = load_source("challenge", join(copypath, "challenge.py"))

    if deployment_directory is None:
        deployment_directory = home_directory

    Problem = update_problem_class(challenge.Problem, problem_object, seed, username, deployment_directory)

    # run methods in proper order
    problem = Problem()

    # reseed and generate flag
    problem.flag = problem.generate_flag(Random(seed))
    logger.debug("...Instance %d flag is '%s'.", instance_number, problem.flag)

    logger.debug("...Running problem initialize.")
    problem.initialize()

    web_accessible_files = []

    def url_for(web_accessible_files, source_name, display=None, raw=False):
        source_path = join(copypath, source_name)

        problem_hash = problem_object["name"] + deploy_config.DEPLOY_SECRET + str(instance_number)
        problem_hash = md5(problem_hash.encode("utf-8")).hexdigest()

        destination_path = join(STATIC_FILE_ROOT, problem_hash, source_name)

        link_template = "<a href='{}'>{}</a>"

        web_accessible_files.append((source_path, join(deploy_config.WEB_ROOT, destination_path)))
        uri_prefix = "//"
        uri = join(uri_prefix, deploy_config.HOSTNAME, destination_path)

        if not raw:
            return link_template.format(uri, source_name if display is None else display)

        return uri

    problem.url_for = functools.partial(url_for, web_accessible_files)

    template_staging_directory(copypath, problem)

    if isinstance(problem, Compiled):
        problem.compiler_setup()
    if isinstance(problem, Remote):
        problem.remote_setup()
    if isinstance(problem, FlaskApp):
        problem.flask_setup()
    if isinstance(problem, PHPApp):
        problem.php_setup()
    if isinstance(problem, Service):
        problem.service_setup()

    logger.debug("...Running problem setup.")
    problem.setup()

    os.chdir(cwd)

    all_files = copy(problem.files)

    if isinstance(problem, Compiled):
        all_files.extend(problem.compiled_files)
    if isinstance(problem, Service):
        all_files.extend(problem.service_files)

    if not all([isinstance(f, File) for f in all_files]):
        logger.error("All files must be created using the File class!")
        raise FatalException

    for f in all_files:
        if not os.path.isfile(join(copypath, f.path)):
            logger.error("File '%s' does not exist on the file system!", f)

    service_file = create_service_file(problem, instance_number, staging_directory)
    logger.debug("...Created service file '%s'.", service_file)

    # template the description
    problem.description = template_string(problem.description, **get_attributes(problem))
    logger.debug("...Instance description: %s", problem.description)

    return {
        "problem": problem,
        "staging_directory": staging_directory,
        "home_directory": home_directory,
        "deployment_directory": deployment_directory,
        "files": all_files,
        "web_accessible_files": web_accessible_files,
        "service_file": service_file
    }

def deploy_problem(problem_directory, instances=[0], test=False, deployment_directory=None):
    """
    Deploys the problem specified in problem_directory.

    Args:
        problem_directory: The directory storing the problem
        instances: The number of instances to deploy. Defaults to 1.
        test: Whether the instances are test instances or not. Defaults to False.
        deployment_directory: If not None, the challenge will be deployed here instead of their home directory
    """

    global current_problem, current_instance

    problem_object = get_problem(problem_directory)

    current_problem = problem_object["name"]

    instance_list = []

    logger.debug("Beginning to deploy problem '%s'.", problem_object["name"])

    for instance_number in instances:
        current_instance = instance_number
        staging_directory = generate_staging_directory()
        if test and deployment_directory is None:
            deployment_directory = join(staging_directory, "deployed")

        instance = generate_instance(problem_object, problem_directory, instance_number, staging_directory, deployment_directory=deployment_directory)
        instance_list.append((instance_number, instance))

    deployment_json_dir = join(DEPLOYED_ROOT, sanitize_name(problem_object["name"]))
    if not os.path.isdir(deployment_json_dir):
        os.makedirs(deployment_json_dir)

    # ensure that the deployed files are not world-readable
    os.chmod(DEPLOYED_ROOT, 0o750)

    # all instances generated without issue. let's do something with them
    for instance_number, instance in instance_list:
        problem_path = join(instance["staging_directory"], PROBLEM_FILES_DIR)
        problem = instance["problem"]
        deployment_directory = instance["deployment_directory"]

        deploy_files(problem_path, deployment_directory, instance["files"], problem.user, problem.__class__)

        if test:
            logger.info("Test instance %d information:", instance_number)
            logger.info("...Description: %s", problem.description)
            logger.info("...Deployment Directory: %s", deployment_directory)

            logger.debug("Cleaning up test instance side-effects.")
            logger.debug("...Killing user processes.")
            #This doesn't look great.
            try:
                execute("killall -u {}".format(problem.user))
                sleep(0.1)
            except RunProcessError as e:
                pass

            logger.debug("...Removing test user '%s'.", problem.user)
            execute(["userdel", problem.user])
            shutil.rmtree(instance["home_directory"])

            deployment_json_dir = instance["staging_directory"]
        else:
            # copy files to the web root
            logger.debug("...Copying web accessible files: %s", instance["web_accessible_files"])
            for source, destination in instance["web_accessible_files"]:
                if not os.path.isdir(os.path.dirname(destination)):
                    os.makedirs(os.path.dirname(destination))
                shutil.copy2(source, destination)

            install_user_service(instance["service_file"])

            # delete staging directory
            shutil.rmtree(instance["staging_directory"])

        unique = problem_object["name"] + problem_object["author"] + str(instance_number) + deploy_config.DEPLOY_SECRET

        deployment_info = {
            "user": problem.user,
            "deployment_directory": deployment_directory,
            "service": os.path.basename(instance["service_file"]),
            "server": problem.server,
            "description": problem.description,
            "flag": problem.flag,
            "instance_number": instance_number,
            "files": [f.to_dict() for f in instance["files"]]
        }

        if isinstance(problem, Service):
            deployment_info["port"] = problem.port
            logger.debug("...Port %d has been allocated.", problem.port)

        instance_info_path = os.path.join(deployment_json_dir, "{}.json".format(instance_number))
        with open(instance_info_path, "w") as f:
            f.write(json.dumps(deployment_info, indent=4, separators=(", ", ": ")))

        logger.debug("The instance deployment information can be found at '%s'.", instance_info_path)

    logger.info("Problem '%s' deployed successfully.", problem_object["name"])

def deploy_problems(args, config):
    """ Main entrypoint for problem deployment """

    global deploy_config, port_map
    deploy_config = config

    try:
        user = getpwnam(deploy_config.DEFAULT_USER)
    except KeyError as e:
        logger.info("DEFAULT_USER '%s' does not exist. Creating the user now.", deploy_config.DEFAULT_USER)
        create_user(deploy_config.DEFAULT_USER)

    if args.deployment_directory is not None and (len(args.problem_paths) > 1 or args.num_instances > 1):
        logger.error("Cannot specify deployment directory if deploying multiple problems or instances.")
        raise FatalException

    if args.secret:
        deploy_config.DEPLOY_SECRET = args.secret
        logger.warn("Overriding DEPLOY_SECRET with user supplied secret '%s'.", args.secret)

    problem_names = args.problem_paths

    if args.bundle:
        bundle_problems = []
        for bundle_path in args.problem_paths:
            if os.path.isfile(bundle_path):
                bundle = get_bundle(bundle_path)
                bundle_problems.extend(bundle["problems"])
            else:
                bundle_sources_path = get_bundle_root(bundle_path, absolute=True)
                if os.path.isdir(bundle_sources_path):
                    bundle = get_bundle(bundle_sources_path)
                    bundle_problems.extend(bundle["problems"])
                else:
                    logger.error("Could not find bundle at '%s'.", bundle_path)
                    raise FatalException
        problem_namess = bundle_problems

    # before deploying problems, load in port_map and already_deployed instances
    already_deployed = {}
    for path, problem in get_all_problems().items():
        already_deployed[path] = []
        for instance in get_all_problem_instances(path):
            already_deployed[path].append(instance["instance_number"])
            if "port" in instance:
                port_map[instance["port"]] = (problem["name"], instance["instance_number"])

    lock_file = join(HACKSPORTS_ROOT, "deploy.lock")
    if os.path.isfile(lock_file):
        logger.error("Cannot deploy while other deployment in progress. If you believe this is an error, "
                         "run 'shell_manager clean'")
        raise FatalException

    with open(lock_file, "w") as f:
        f.write("1")

    if args.instance:
        instance_range = list(range(args.instance, args.instance+1))
    else:
        instance_range = list(range(0, args.num_instances))

    try:
        for problem_name in problem_names:
            if args.redeploy:
                instance_list = instance_range
            else:
                # remove already deployed instances
                instance_list = list(set(instance_range) - set(already_deployed.get(problem_name, [])))

            if args.dry and isdir(problem_name):
                deploy_problem(problem_name, instances=instance_list, test=args.dry,
                                deployment_directory=args.deployment_directory)
            elif isdir(join(get_problem_root(problem_name, absolute=True))):
                deploy_problem(join(get_problem_root(problem_name, absolute=True)), instances=instance_list,
                                test=args.dry, deployment_directory=args.deployment_directory)
            else:
                logger.error("Problem '%s' doesn't appear to be installed.", problem_name)
                raise FatalException
    finally:
        if not args.dry:
            os.remove(lock_file)

def remove_instances(path, instance_list):
    """ Remove all files under deployment directory and metdata for a given list of instances """

    problem_instances = get_all_problem_instances(path)
    deployment_json_dir = join(DEPLOYED_ROOT, path)

    for instance in problem_instances:
        instance_number = instance["instance_number"]
        if instance["instance_number"] in instance_list:
            logger.debug("Removing instance {} of '{}'.".format(instance_number, path))

            directory = instance["deployment_directory"]
            user = instance["user"]
            service = instance["service"]
            deployment_json_path = join(deployment_json_dir, "{}.json".format(instance_number))

            logger.debug("...Removing systemd service '%s'.", service)
            execute(["systemctl", "stop", service], timeout=60)
            execute(["systemctl", "disable", service], timeout=60)
            os.remove(join(SYSTEMD_SERVICE_PATH, service))
            os.remove(deployment_json_path)


            logger.debug("...Removing deployment directory '%s'.", directory)
            shutil.rmtree(directory)

            logger.debug("...Removing problem user '%s'.", user)
            execute(["userdel", user])

def undeploy_problems(args, config):
    """ Main entrypoint for problem undeployment """

    problem_names = args.problem_paths

    if args.bundle:
        bundle_problems = []
        for bundle_path in args.problem_paths:
            if isfile(bundle_path):
                bundle = get_bundle(bundle_path)
                bundle_problems.extend(bundle["problems"])
            else:
                bundle_sources_path = get_bundle_root(bundle_path, absolute=True)
                if isdir(bundle_sources_path):
                    bundle = get_bundle(bundle_sources_path)
                    bundle_problems.extend(bundle["problems"])
                else:
                    logger.error("Could not find bundle at '%s'.", bundle_path)
                    raise FatalException
        problem_names = bundle_problems

    # before deploying problems, load in already_deployed instances
    already_deployed = {}
    for path, problem in get_all_problems().items():
        already_deployed[problem["name"]] = []
        for instance in get_all_problem_instances(path):
            already_deployed[problem["name"]].append(instance["instance_number"])

    lock_file = join(HACKSPORTS_ROOT, "deploy.lock")
    if os.path.isfile(lock_file):
        logger.error("Cannot undeploy while other deployment in progress. If you believe this is an error, "
                         "run 'shell_manager clean'")
        raise FatalException

    with open(lock_file, "w") as f:
        f.write("1")

    if args.instance:
        instance_range = list(range(args.instance, args.instance+1))
    else:
        instance_range = list(range(0, args.num_instances))

    try:
        for problem_name in problem_names:
            problem_root = get_problem_root(problem_name, absolute=True)
            if isdir(problem_root):
                problem = get_problem(problem_root)
                instances = list(filter(lambda x: x in already_deployed[problem["name"]], instance_range))
                if len(instances) == 0:
                    logger.warn("No deployed instances %s were found for problem '%s'.", instance_range, problem["name"])
                else:
                    logger.debug("Undeploying problem '%s'.", problem["name"])
                    remove_instances(problem_name, instance_range)
                    logger.info("Problem instances %s were successfully removed from '%s'.", instances, problem["name"])
            else:
                logger.error("Problem '%s' doesn't appear to be installed.", problem_name)
                raise FatalException
    finally:
        os.remove(lock_file)
