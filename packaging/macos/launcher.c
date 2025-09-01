#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <string.h>
#include <sys/stat.h>

int main(int argc, char *argv[]) {
    char app_path[1024];
    char resources_path[1024];
    char script_path[1024];
    char *executable_path;
    
    // Get the path to this executable
    executable_path = realpath(argv[0], NULL);
    if (!executable_path) {
        fprintf(stderr, "Error: Could not resolve executable path\n");
        return 1;
    }
    
    // Construct paths
    snprintf(app_path, sizeof(app_path), "%s", executable_path);
    char *contents_pos = strstr(app_path, "/Contents/");
    if (contents_pos) {
        *contents_pos = '\0';
    }
    
    snprintf(resources_path, sizeof(resources_path), "%s/Contents/Resources", app_path);
    snprintf(script_path, sizeof(script_path), "%s/Contents/Resources/launcher.sh", app_path);
    
    // Check if the launcher script exists
    struct stat st;
    if (stat(script_path, &st) != 0) {
        fprintf(stderr, "Error: Launcher script not found at %s\n", script_path);
        free(executable_path);
        return 1;
    }
    
    // Change to resources directory
    if (chdir(resources_path) != 0) {
        fprintf(stderr, "Error: Could not change to resources directory\n");
        free(executable_path);
        return 1;
    }
    
    // Execute the launcher script
    execl(script_path, script_path, NULL);
    
    // If we get here, execl failed
    fprintf(stderr, "Error: Failed to execute launcher script\n");
    free(executable_path);
    return 1;
}
