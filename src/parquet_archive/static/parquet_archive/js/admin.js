'use strict';
{
    // NOTE: On creation the field choices depend on the chosen model, so the form is
    // reloaded with ?content_type=<id> (the admin uses the URL parameters as initial values)
    window.addEventListener('load', function() {
        const contentType = document.getElementById('id_content_type');
        if (!contentType) {
            return;
        }

        django.jQuery(contentType).select2().on('change', function() {
            const params = new URLSearchParams(window.location.search);
            params.set('content_type', contentType.value);
            window.location.search = params.toString();
        });
    });
}
