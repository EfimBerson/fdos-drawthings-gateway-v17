#include <unistd.h>
#include <stdio.h>

int main(void) {
    char *args[] = {
        "/usr/bin/python3",
        "/Users/efim/Apps/fdos-drawthings-gateway-v17/gateway.py",
        NULL
    };

    execv("/usr/bin/python3", args);
    perror("FDOS Draw Things Gateway");
    return 127;
}
