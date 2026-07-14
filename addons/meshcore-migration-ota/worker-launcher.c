#include <stdio.h>
#include <unistd.h>

int main(void) {
    char *const argv[] = {
        "/opt/venv/bin/python3",
        "-I",
        "-B",
        "-u",
        "-c",
        "import runpy,sys;sys.path.insert(0,'/usr/share/meshcore-migration-ota');runpy.run_module('app.worker',run_name='__main__')",
        NULL,
    };
    char *const envp[] = {
        "PATH=/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE=1",
        "PYTHONHASHSEED=random",
        NULL,
    };

    execve(argv[0], argv, envp);
    perror("execve");
    return 111;
}
