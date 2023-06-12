"""
Script to install, start, stop, reset, etc. SIMOC through docker-compose.
"""

import os
import sys
import time
import json
import shutil
import socket
import pathlib
import argparse
import subprocess


ENV_FILE = 'simoc_docker.env'
AGENT_DESC = 'data_files/agent_desc.json'

COMPOSE_FILE = 'docker-compose.mysql.yml'
DEV_FE_COMPOSE_FILE = 'docker-compose.dev-fe.yml'
DEV_BE_COMPOSE_FILE = 'docker-compose.dev-be.yml'
AGENT_DESC_COMPOSE_FILE = 'docker-compose.agent-desc.yml'
TESTING_COMPOSE_FILE = 'docker-compose.testing.yml'
DOCKER_COMPOSE_CMD = ['docker-compose', '-f', COMPOSE_FILE]

CURRENT_PATH = pathlib.Path.cwd().resolve().name
NETWORK_NAME = f'{CURRENT_PATH}_simoc-net'
DB_NAME = f'{CURRENT_PATH}_simoc-db'
DB_VOLUME = f'{CURRENT_PATH}_db-data'
DB_TESTING = f'{CURRENT_PATH}_db-testing'
FLASK_CONTAINER = f'{CURRENT_PATH}_flask'
CELERY_CONTAINER = f'{CURRENT_PATH}_celery'
REDIS_CONTAINER = f'{CURRENT_PATH}_redis'
NGINX_CONTAINER = f'{CURRENT_PATH}_nginx'


def parse_env(fname):
    env = {}
    with open(fname) as f:
        for line in map(str.strip, f):
            if not line or line.startswith('#'):
                continue
            if line.startswith('export'):
                key, value = line.split(None, 1)[1].split('=', 1)
                env[key] = value.strip('"\'')
            else:
                print(f'Unrecognized line in {fname}: {line!r}')
    return env

def update_env(env_file):
    try:
        envvars = parse_env(env_file)
    except FileNotFoundError:
        sys.exit(f"Can't find env file: {env_file!r}")
    # update environ with the new envvars
    os.environ.update(envvars)
    return envvars

COMMANDS = {}

def cmd(func):
    """Decorator to add commands to the COMMANDS dict."""
    COMMANDS[func.__name__] = func
    return func

def run(args):
    print('>'*80)
    print(' '.join(args))
    print('-'*80)
    result = subprocess.run(args, env=os.environ)
    print('-'*80)
    print(result)
    print('<'*80)
    print()
    return not result.returncode

def docker_available():
    """Return True if docker and docker-compose are installed."""
    return shutil.which('docker') and shutil.which('docker-compose')

@cmd
def docker(*args):
    """Run an arbitrary docker command."""
    if not docker_available():
        install_docker()
    return run(['docker', *args])

@cmd
def docker_compose(*args):
    """Run an arbitrary docker-compose command."""
    if not docker_available():
        install_docker()
    # if the docker-compose file is missing, create it
    if not pathlib.Path(COMPOSE_FILE).exists():
        generate_scripts()
    return run([*DOCKER_COMPOSE_CMD, *args])

@cmd
def print_env():
    """Print a copy-pastable list of envvars."""
    for key, value in ENVVARS.items():
        print(f'export {key}={value!r}')
    return True


# initial setup
def install_docker():
    """Install docker and docker-compose."""
    return install_docker_linux()

def install_docker_linux():
    # this function is duplicated in overthesun/simoc-web/simoc-web.py,
    # changes to this function should be ported there too
    if docker_available():
        return True
    # `apt` already creates a `docker` group, but we need to manually
    # add the current user to it and ask the user to log out/log in
    # for the change to take place and for `docker` to work without `sudo`
    print('Installing docker and docker-compose:')
    user = os.getenv('USER')
    if not (run(['sudo', 'apt', 'install', '-y', 'docker', 'docker-compose']) and
            run(['sudo', 'usermod', '-aG', 'docker', user])):
        return False
    print('Please log out and log in again (or restart the machine) '
          'to complete the Docker installation.')
    print('After logging back in, you can resume the installation of SIMOC.')
    print()
    return False

def install_jinja():
    """Install Jinja2."""
    try:
        import jinja2
        return True  # Jinja already installed
    except ImportError:
        print('Installing Jinja2:')
        return run(['sudo', 'apt', 'install', '-y', 'python3-jinja2'])

@cmd
def install_deps():
    """Install dependencies needed by SIMOC."""
    return install_jinja() and install_docker()

@cmd
def generate_scripts():
    """Generate simoc_nginx.conf and docker-compose.mysql.yml."""
    install_jinja()
    import generate_docker_configs
    return generate_docker_configs.main(CURRENT_PATH)

@cmd
def make_cert():
    """Create the certs/cert.pem SSL certificate."""
    pathlib.Path('certs').mkdir(exist_ok=True)
    #print('Creating SSL certificates.  Use the following values:',
          #'Country Name (2 letter code) []:US',
          #'State or Province Name (full name) []:Texas',
          #'Locality Name (eg, city) []:Austin',
          #'Organization Name (eg, company) []:SIMOC',
          #'Organizational Unit Name (eg, section) []:',
          #f'Common Name (eg, fully qualified host name) []:{domain}',
          #'Email Address []:', sep='\n')
    certpath = 'certs/cert.pem'
    if socket.gethostname().endswith('simoc.space'):
        domain = 'beta.simoc.space'
    else:
        domain = 'localhost'
    return run(['openssl', 'req', '-x509', '-newkey', 'rsa:4096', '-nodes',
                '-out', certpath, '-keyout', 'certs/key.pem', '-days', '365',
                '-subj', f"/C=US/ST=Texas/L=Austin/O=SIMOC/CN={domain}"])

@cmd
def build_images():
    """Build the flask and celery images locally."""
    return (docker('build', '-t', FLASK_CONTAINER, '.') and
            docker('build', '-f', 'Dockerfile-celery-worker',
                   '-t', CELERY_CONTAINER, '.'))

@cmd
def start_services():
    """Starts the services."""
    return docker_compose('up', '-d', '--force-recreate')

# DB
@cmd
def init_db():
    """Initialize the MySQL DB."""
    print('Creating DB.  This might take a while...\n')
    attempts = 15
    for attempt in range(15):
        result = docker_compose('exec', 'celery-worker', 'python3', 'create_db.py')
        if result is True:
            return result
        else:
            print('create_db.py failed: if the error above says:\n'
                  '  "Can\'t connect to MySQL server on \'simoc-db\' (111)"\n'
                  'this is expected -- it might take a few minutes before the '
                  'db is up and running. Another attempt will be made in 15s.\n')
            time.sleep(15)
    else:
        print(f'Giving up after {attempts} attempts.  Run the above command '
              f'manually to try again.')
    return False

@cmd
def remove_db():
    """Remove the volume for the MySQL DB."""
    docker_compose('rm', '--stop', '-v', '-f', 'simoc-db')
    docker('volume', 'rm', DB_VOLUME)
    return True  # the volume rm might return False if the volume is missing


@cmd
def reset_db():
    """Remove and recreate the MySQL DB."""
    # the up is needed to update the simoc source code
    # in the containers before rebuilding the db,
    # restarting nginx is needed to make sure the
    # ips of the flask/celery containers are updated
    return (up() and restart('nginx') and remove_db() and
            docker_compose('up', '-d', '--force-recreate', 'simoc-db') and
            init_db())


# start/stop
@cmd
def up(*args):
    """Start/update the containers with `docker-compose up -d`."""
    return docker_compose('up', '-d', *args)

@cmd
def down(*args):
    """Stop/remove the containers with `docker-compose down`."""
    return docker_compose('down', *args)

@cmd
def restart(*args):
    """Restart the containers with `docker-compose restart`."""
    return docker_compose('restart', *args)


# status and logging
@cmd
def ps(*args):
    """Run `docker-compose ps`."""
    return docker_compose('ps', *args)

@cmd
def logs(*args):
    """Show all logs."""
    return docker_compose('logs', '-f', *args)

@cmd
def celery_logs(*args):
    """Show the celery logs."""
    return docker_compose('logs', '-f', 'celery-worker', *args)

@cmd
def flask_logs(*args):
    """Show the flask logs."""
    return docker_compose('logs', '-f', 'flask-app', *args)


# install/uninstall
def post_setup_msg():
    """Print a message and ask to open SIMOC in the browser."""
    print('Setup completed!')
    if pathlib.Path('simoc_server', 'dist').exists():
        url = f'http://localhost:{os.environ["HTTP_PORT"]}/'
        print(f'You can now access SIMOC at <{url}>.' )
        ans = input('Do you want to open SIMOC on your browser? [Y/n] ')
        if ans.lower().strip() != 'n':
            import webbrowser
            webbrowser.open_new_tab(url)
    return True

@cmd
def setup():
    """Run a complete setup of SIMOC."""
    return (install_deps() and generate_scripts() and make_cert() and
            build_images() and start_services() and init_db() and
            ps() and post_setup_msg())

@cmd
def teardown():
    """Remove simoc and all related containers/images/volumes"""
    return docker_compose('down', '--rmi', 'all',
                          '--volumes', '--remove-orphans')

@cmd
def reset():
    """Remove everything, then run a full setup."""
    return teardown() and setup()


# testing/debugging
@cmd
def test(*args):
    """Run the tests in the container."""
    # add the testing yml that replaces the db
    DOCKER_COMPOSE_CMD.extend(['-f', TESTING_COMPOSE_FILE])
    # check if the volume already exists
    cp = subprocess.run(['docker', 'volume', 'inspect', DB_TESTING],
                        capture_output=True)
    vol_info = json.loads(cp.stdout)
    if not vol_info:
        # volume doesn't exist -- create it and init the db
        init_test_db = init_db
    else:
        # volume exist and should be Initialized -- do nothing
        init_test_db = lambda: True
    return (up() and init_test_db() and
            docker_compose('exec', 'flask-app',
                                   'pytest', '-v', '--pyargs', '--durations=0',
                                   'simoc_server', *args))

@cmd
def shell(container, *args):
    """Start a shell in the given container."""
    return docker_compose('exec', container, '/bin/bash', *args)

@cmd
def adminer(db=None):
    """Start an adminer container to inspect the DB."""
    if db and 'testing' in db:
        # mount the 'simoc_db-testing' volume instead of the
        # default 'simoc_db-data' if 'testing' is passed as arg
        DOCKER_COMPOSE_CMD.extend(['-f', TESTING_COMPOSE_FILE])
    def show_info():
        # show the volume that is currently connected to the db container
        cp = subprocess.run(['docker', 'inspect', '-f',
                             '{{range .Mounts}}{{.Name}}{{end}}',
                             f'{DB_NAME}_1'], capture_output=True)
        print('* Starting adminer at: http://localhost:8081/')
        print('* Connecting to:', cp.stdout.decode('utf-8'))
        return True
    cmd = ['run', '--network', NETWORK_NAME,
           '--link', '{DB_NAME}_1:db', '-p', '8081:8080',
           '-e', 'ADMINER_DESIGN=dracula', 'adminer']
    return up() and show_info() and docker(*cmd)


# Jupyter Notebook environment
# TODO: Needs Fixing
def launch_env(envname):
    """Launch simoc virtualenv"""
    return run(['python3', f'{envname}/bin/activate_this.py'])

def setup_env(envname, kernelname):
    """Create simoc virtualenv, install packages and ipython kernel"""
    run(['virtualenv', envname])
    launch_env(envname)
    print("* Installing SIMOC requirements...")
    run(['pip', 'install', '-r', 'requirements-jupyter.txt'])
    run(['ipython', 'kernel', 'install', '--name', kernelname, '--user'])

@cmd
def jupyter(envname='simoc_env', kernelname='simoc'):
    """Open jupyter notebook in virtualenv"""
    if not pathlib.Path(envname).exists():
        setup_env(envname, kernelname)
    launch_env(envname)
    return run(['jupyter', 'notebook', f'--GatewayKernelSpecManager.allowed_kernelspecs={kernelname}'])

# others
@cmd
def format_agent_desc(fname=AGENT_DESC):
    """Reformat agent_desc.json files (e.g. the one generated by ACE)."""
    print(f'Reformatting <{fname}>...', end=' ')
    with open(fname) as f:
        ad = json.load(f)
    with open(fname, 'w', newline='\r\n') as f:
        json.dump(ad, f, indent=2)
    print('done.')


# create the list of commands
def create_help(cmds):
    help = ['Full list of available commands:']
    for cmd, func in cmds.items():
        help.append(f'{cmd.replace("_", "-"):18} {func.__doc__}')
    return '\n'.join(help)


if __name__ == '__main__':
    desc = """\
Simplify installation and maintainment of SIMOC.

Use `setup` to install SIMOC, `teardown` to uninstall everything.
Use `reset` to reinstall (same as `teardown` + `setup`).
Use `up` to start/update the containers, `down` to stop/remove them.
Use `logs`, `flask-logs`, `celery-logs`, to see the logs.
Use the `--with-dev-backend` flag to run the dev backend container.
"""
    parser = argparse.ArgumentParser(
        description=desc,
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        '--docker-file', metavar='FILE', default=COMPOSE_FILE,
        help='the docker-compose yml file (default: %(default)r)'
    )
    parser.add_argument(
        '--env-file', metavar='FILE', default=ENV_FILE,
        help='the env file (default: %(default)r)'
    )
    parser.add_argument(
        '--with-dev-frontend', action='store_true',
        help='also start the dev frontend container'
    )
    parser.add_argument(
        '--dev-frontend-yml', metavar='FILE',
        help='the dev frontend docker-compose yml file'
    )
    parser.add_argument(
        '--dev-frontend-dir', metavar='DIR',
        help='the dir where the dev frontend code is'
    )
    parser.add_argument(
        '--with-dev-backend', action='store_true',
        help='use the dev backend flask container'
    )
    parser.add_argument(
        '--dev-backend-yml', metavar='FILE',
        help='the dev backend docker-compose yml file'
    )
    parser.add_argument(
        '--agent-desc', metavar='FILE',
        help='the agent_desc.json file to be used'
    )
    parser.add_argument('cmd', metavar='CMD', help=create_help(COMMANDS))
    parser.add_argument('args', metavar='*ARGS', nargs='*',
                        help='Additional optional args to be passed to CMD.')
    args = parser.parse_args()

    if args.docker_file:
        COMPOSE_FILE = args.docker_file
        DOCKER_COMPOSE_CMD = ['docker-compose', '-f', COMPOSE_FILE]

    if args.env_file:
        ENV_FILE = args.env_file
    ENVVARS = update_env(ENV_FILE)

    if (args.dev_frontend_dir or args.dev_frontend_yml) and not args.with_dev_frontend:
        parser.error("Can't specify the dev frontend dir/yml without --with-dev-frontend")

    if args.dev_backend_yml and not args.with_dev_backend:
        parser.error("Can't specify the dev backend yml without --with-dev-backend")

    if args.with_dev_frontend:
        if args.dev_frontend_dir:
            os.environ['DEV_FE_DIR'] = args.dev_frontend_dir
        if not os.environ['DEV_FE_DIR']:
            parser.error('Please specify the dev frontend dir (either in '
                         'simoc_docker.env or with --dev-frontend-dir).')
        yml_file = args.dev_frontend_yml or DEV_FE_COMPOSE_FILE
        DOCKER_COMPOSE_CMD.extend(['-f', yml_file])

    if args.with_dev_backend:
        yml_file = args.dev_backend_yml or DEV_BE_COMPOSE_FILE
        DOCKER_COMPOSE_CMD.extend(['-f', yml_file])

    if args.agent_desc and args.with_dev_backend:
        parser.error("Can't specify the agent_desc.json file with --with-dev-backend")

    if args.agent_desc:
        os.environ['AGENT_DESC'] = str(pathlib.Path(args.agent_desc).resolve())
        DOCKER_COMPOSE_CMD.extend(['-f', AGENT_DESC_COMPOSE_FILE])

    cmd = args.cmd.replace('-', '_')
    if cmd in COMMANDS:
        result = COMMANDS[cmd](*args.args)
        parser.exit(not result)
    else:
        cmds = ', '.join(cmd.replace('_', '-') for cmd in COMMANDS.keys())
        parser.error(f'Command not found.  Available commands: {cmds}')
