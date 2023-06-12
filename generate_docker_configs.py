import os
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined


def main(current_path, env=os.environ):
    j2_env = Environment(loader=FileSystemLoader('./'),
                         trim_blocks=True, lstrip_blocks=True,
                         undefined=StrictUndefined)
    config = {
        "current_path": current_path,
        "version": env.get('VERSION', 'latest'),
        "use_dockerhub": int(env.get('USE_DOCKERHUB', False)),
        "docker_repo": env.get('DOCKER_REPO', ''),
        "server_name": env.get('SERVER_NAME', 'localhost'),
        "use_ssl": int(env.get('USE_SSL', False)),
        "http_port": env.get('HTTP_PORT', 8000),
        "https_port": env.get('HTTPS_PORT', 8443),
        "use_certbot": int(env.get('USE_CERTBOT', False)),
        "redis_use_bitnami": int(env.get('REDIS_USE_BITNAMI', 1)),
        "redirect_to_ssl": int(env.get('REDIRECT_TO_SSL', False)),
        "add_basic_auth": int(env.get('ADD_BASIC_AUTH', False)),
        "use_node_dev": int(env.get('USE_NODE_DEV', False)),
        "node_dev_dir": env.get('NODE_DEV_DIR', ''),
        "valid_referers": env.get('VALID_REFERERS', ''),
    }
    nginx_jinja = Path('nginx/simoc_nginx.conf.jinja')
    docker_compose_jinja = Path('docker-compose.mysql.yml.jinja')
    print('Generating config files:')
    for jinja_file in [nginx_jinja, docker_compose_jinja]:
        temp_file = j2_env.get_template(str(jinja_file))
        conf_file = jinja_file.parent / jinja_file.stem  # remove .jinja suffix
        with open(conf_file, 'w') as f:
            f.write(temp_file.render(**config))
        print(f'  * <{conf_file}> created')
    print()
    return True


if __name__ == "__main__":
    main()
