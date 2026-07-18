/* Minimal PAM module mocking a 2FA challenge for sshPilot integration tests.
 *
 * Prompts "Verification code: " over the PAM conversation (keyboard-
 * interactive) and accepts the fixed code 123456. Stacked after pam_unix in
 * /etc/pam.d/sshd so a login requires password + code — a deterministic
 * stand-in for TOTP/Duo without time-based state.
 */
#include <security/pam_appl.h>
#include <security/pam_modules.h>
#include <stdlib.h>
#include <string.h>

#define MOCK_CODE "123456"

int pam_sm_authenticate(pam_handle_t *pamh, int flags, int argc,
                        const char **argv) {
    (void)flags; (void)argc; (void)argv;
    const struct pam_conv *conv = NULL;
    if (pam_get_item(pamh, PAM_CONV, (const void **)&conv) != PAM_SUCCESS ||
        conv == NULL || conv->conv == NULL)
        return PAM_AUTH_ERR;

    struct pam_message msg;
    memset(&msg, 0, sizeof(msg));
    msg.msg_style = PAM_PROMPT_ECHO_OFF;
    msg.msg = "Verification code: ";
    const struct pam_message *msgp = &msg;
    struct pam_response *resp = NULL;

    if (conv->conv(1, &msgp, &resp, conv->appdata_ptr) != PAM_SUCCESS ||
        resp == NULL)
        return PAM_AUTH_ERR;

    int ok = resp[0].resp != NULL && strcmp(resp[0].resp, MOCK_CODE) == 0;
    if (resp[0].resp != NULL) {
        memset(resp[0].resp, 0, strlen(resp[0].resp));
        free(resp[0].resp);
    }
    free(resp);
    return ok ? PAM_SUCCESS : PAM_AUTH_ERR;
}

int pam_sm_setcred(pam_handle_t *pamh, int flags, int argc, const char **argv) {
    (void)pamh; (void)flags; (void)argc; (void)argv;
    return PAM_SUCCESS;
}
