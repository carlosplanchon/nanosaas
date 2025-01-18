# You need to use -p (project name). That's to avoid conflicts with other docker-compose projects.
# When you use the -p (project name) flag in docker-compose,
# it affects the naming convention of the Docker resources (containers, networks, and volumes)
# by using the specified project name as a prefix. This helps to avoid conflicts
# between multiple projects running on the same system.

docker-compose -p nanosaas up -d

# To stop the service write:
# docker compose -p nanosaas down
