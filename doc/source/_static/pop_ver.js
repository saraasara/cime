$(document).ready(function() {
    $(".version").html("<div id='version-dropdown'><select id='version-list'></select></div>");

    var versionList = $("#version-list");
    var baseUriRegex = /(.*\/cime\/versions).*/g;
    //var baseUri = baseUriRegex.exec("https://esmci.github.io/cime/versions/master/html/index.html");
    var baseUri = baseUriRegex.exec(document.baseURI);

    if (baseUri == undefined && baseUri.length != 2) {
        return;
    }

    var version_json_loc = baseUri[1] + "/versions.json";

    versionList.change(function() {
        window.location = this.value;
    });

    $.getJSON(version_json_loc, function(data) {
        $.each(data, function(version_dir, version_name) {
            versionList.append($("<option>", {value: baseUri[1] + "/" + version_dir + "/html/index.html", text: version_name}));
        });
    });
});
